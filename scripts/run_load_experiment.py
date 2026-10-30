#!/usr/bin/env python3
"""Ramp concurrent users against the deployed service and record where it starts to fail.

Unlike the fault scenarios this injects no fault. It answers the question the
fault scenarios cannot: does the service keep answering when more people use it
at once. A user here sends one request, waits for the answer, and sends the
next one no sooner than ``--think-seconds`` after the previous one started.

The generator runs as a Pod inside the request namespace so the measurement
does not include kubectl port-forward. Its own CPU limit is recorded with the
result, because a starved generator would look like a slow service.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from collect_baseline import kubectl_command, kubectl_environment
from in_cluster_probe import InClusterProbe
from run_litmus_pod_delete import measured_recovery_seconds
from run_pod_kill_experiment import get_target_configuration, metrics_from_probes

GENERATOR_RESOURCES = {'requests': {'cpu': '250m', 'memory': '64Mi'},
                       'limits': {'cpu': '1000m', 'memory': '256Mi'}}

# Runs inside python:3.12-alpine, the image the in-cluster observer already uses.
# A request fails when no HTTP answer arrives in time, or the answer is 5xx or 429:
# those are what a user sees as "the service is down or turned me away".
# Other 4xx (auth, missing route) are answers from a running service.
GENERATOR_CODE = r'''
import http.client,json,os,random,threading,time
from urllib.parse import urlsplit
URL=urlsplit(os.environ["TARGET_URL"]); STAGES=[int(v) for v in os.environ["STAGES"].split(",")]
STAGE_SECONDS=float(os.environ["STAGE_SECONDS"]); THINK=float(os.environ["THINK_SECONDS"]); TIMEOUT=float(os.environ["REQUEST_TIMEOUT"])
PATH=(URL.path or "/")+("?"+URL.query if URL.query else "")
def percentile(values,p):
 if not values: return None
 values=sorted(values); return round(values[min(len(values)-1,int(round(p/100*(len(values)-1))))],2)
def request():
 # http.client, not urllib: building a urllib opener per user took up to 3s each
 # under 100 threads and halved the offered load (measured locally).
 connection=http.client.HTTPConnection(URL.hostname,URL.port or 80,timeout=TIMEOUT)
 try:
  connection.request("GET",PATH,headers={"Connection":"close"})
  response=connection.getresponse(); response.read(65536); return response.status
 finally: connection.close()
def stage(users):
 records=[]; lock=threading.Lock(); go=threading.Event(); window={}
 def user():
  go.wait(); time.sleep(random.random()*THINK)
  while time.monotonic()<window["stop"]:
   started=time.monotonic(); status=None; error=None
   try: status=request()
   except Exception as exc: error=type(exc).__name__
   latency=(time.monotonic()-started)*1000
   with lock: records.append((latency,status,error))
   time.sleep(max(0.0,THINK-(time.monotonic()-started)))
 threads=[threading.Thread(target=user,daemon=True) for _ in range(users)]
 for t in threads: t.start()
 begun=time.monotonic(); window["stop"]=begun+STAGE_SECONDS; go.set()
 for t in threads: t.join(STAGE_SECONDS+TIMEOUT+THINK+5)
 elapsed=time.monotonic()-begun
 failed=[r for r in records if r[2] is not None or r[1] is None or r[1]>=500 or r[1]==429]
 statuses={}; errors={}
 for latency,status,error in records:
  if status is not None: statuses[str(status)]=statuses.get(str(status),0)+1
  if error: errors[error]=errors.get(error,0)+1
 answered=[r[0] for r in records if r[2] is None]
 return {"users":users,"duration_seconds":round(elapsed,2),"requests":len(records),"failed_requests":len(failed),
  "error_rate":round(len(failed)/len(records),4) if records else None,
  "throughput_rps":round(len(records)/elapsed,2) if elapsed else None,"offered_rps":round(users/THINK,2),
  "p50_latency_ms":percentile(answered,50),"p95_latency_ms":percentile(answered,95),"p99_latency_ms":percentile(answered,99),
  "status_counts":statuses,"error_counts":errors,"unfinished_users":sum(t.is_alive() for t in threads)}
print(json.dumps({"stages":[stage(users) for users in STAGES]}),flush=True)
'''


def main() -> int:
    args = parse_args()
    target = {'namespace': args.namespace, 'deployment': args.deployment, 'service': args.service,
              'servicePort': args.service_port, 'labelSelector': args.label_selector}
    stages = with_target(parse_stages(args.stages), args.target_users)
    configuration = get_target_configuration(args.namespace, args.deployment)
    restarts_before = restart_count(args.namespace, args.label_selector)
    url = f'http://{args.service}.{args.namespace}.svc.cluster.local:{args.service_port}{args.probe_path}'

    with InClusterProbe(args.namespace, args.service, args.service_port, local_port=args.local_port,
                        timeout=args.request_timeout_seconds, path=args.probe_path) as observer:
        baseline = {'probes': observer.collect(args.baseline_probes)}
        if not all(p['success'] for p in baseline['probes']):
            raise RuntimeError('Load baseline is unhealthy; load skipped.')
        started_at = now_utc()
        load_started = time.monotonic()
        load = run_generator(args, url, stages)
        load_seconds = round(time.monotonic() - load_started, 2)
        recovery_probes, recovered = observer.collect_until_healthy(
            args.recovery_timeout_seconds, args.recovery_probes)
    restarts_after = restart_count(args.namespace, args.label_selector)

    summary = summarize(load['stages'], args.target_users)
    denominator = 'in-cluster HTTP probes before and after the load; load requests are reported per stage'
    baseline_metrics = metrics_from_probes(baseline['probes'])
    baseline_metrics['error_rate_denominator'] = 'HTTP probes collected before the load'
    observed_metrics = metrics_from_probes([*baseline['probes'], *recovery_probes])
    observed_metrics['error_rate_denominator'] = denominator
    output = {
        'schemaVersion': 'chaos-v1',
        'scenario': 'load_concurrent_users',
        'observationStatus': 'observed',
        'exitCode': 0 if recovered else 1,
        'timedOut': not recovered,
        'probeTransport': 'in_cluster_http',
        'probes': {'baseline': baseline['probes'], 'experiment': recovery_probes},
        'target': target,
        'baseline': {'metrics': baseline_metrics},
        'metrics': observed_metrics,
        'chaos_observation': {
            'type': 'load_concurrent_users',
            'kill_method': 'in_cluster_load_generator',
            'started_at': started_at,
            'recovered': recovered,
            'recovered_at': now_utc() if recovered else None,
            # Time the service needed to answer again after the load stopped. Zero when it never stopped answering.
            'recovery_seconds': measured_recovery_seconds(recovery_probes),
            'experiment_duration_seconds': load_seconds,
            'target_configuration': configuration,
            'restart_count_delta': (restarts_after - restarts_before
                                    if restarts_before is not None and restarts_after is not None else None),
            'load': {
                'stages': load['stages'],
                **summary,
                'request_path': args.probe_path,
                'think_seconds': args.think_seconds,
                'stage_seconds': args.stage_seconds,
                'request_timeout_seconds': args.load_timeout_seconds,
                'failure_definition': 'no HTTP answer within the timeout, or status 5xx or 429',
                'generator_resources': GENERATOR_RESOURCES,
            },
            'observation_window': {
                'baseline_probe_count': args.baseline_probes,
                'recovery_timeout_seconds': args.recovery_timeout_seconds,
                'error_rate_denominator': denominator,
            },
            'abort_condition': {'triggered': False, 'reason': None},
        },
        'source': {'real_execution_observed': True, 'target': args.deployment},
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if recovered else 1


def with_target(stages: list[int], target: int | None) -> list[int]:
    """The requester's expected users are always measured exactly, not interpolated between stages."""
    return sorted({*stages, target}) if target else stages


def summarize(stages: list[dict[str, Any]], target_users: int | None = None) -> dict[str, Any]:
    """Where the service stopped answering everyone. No latency threshold is applied here.

    With target_users, target_met says whether every stage up to that many users answered
    everyone. The requester set that number, so the AI may turn a miss into a Fail.
    """
    clean = [s['users'] for s in stages if s.get('requests') and s.get('failed_requests') == 0]
    failing = next((s for s in stages if s.get('failed_requests')), None)
    first_failing = failing['users'] if failing else None
    return {
        # Highest stage below the first failing one. A clean stage after a failing one does not count.
        'max_users_without_errors': max((u for u in clean if first_failing is None or u < first_failing),
                                        default=None),
        'first_users_with_errors': first_failing,
        'all_stages_clean': failing is None and bool(clean),
        'latency_growth': latency_growth(stages),
        'overload_behavior': overload_behavior(failing),
        'target_users': target_users,
        'target_met': (None if not target_users or not any(s['users'] == target_users for s in stages)
                       else first_failing is None or first_failing > target_users),
    }


def latency_growth(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """p95 at the first stage against the last stage that still got answers. A fact, not a verdict."""
    answered = [s for s in stages if s.get('p95_latency_ms') is not None]
    if len(answered) < 2:
        return None
    first, last = answered[0], answered[-1]
    ratio = round(last['p95_latency_ms'] / first['p95_latency_ms'], 1) if first['p95_latency_ms'] else None
    return {'from_users': first['users'], 'from_p95_ms': first['p95_latency_ms'],
            'to_users': last['users'], 'to_p95_ms': last['p95_latency_ms'], 'ratio': ratio}


def overload_behavior(stage: dict[str, Any] | None) -> dict[str, Any] | None:
    """How the first failing stage failed.

    A service that answers 429 or 503 quickly is shedding load and comes back when the
    load drops. One that holds requests until they time out, or stops accepting
    connections, makes every user wait or fail. The difference is what to fix.
    """
    if not stage:
        return None
    statuses = stage.get('status_counts') or {}
    errors = stage.get('error_counts') or {}
    counts = {
        'rejected': sum(n for code, n in statuses.items() if code == '429' or code.startswith('5')),
        'timed_out': sum(n for name, n in errors.items() if 'timeout' in name.lower()),
        'connection_failed': sum(n for name, n in errors.items() if 'timeout' not in name.lower()),
    }
    kind = max(counts, key=lambda key: counts[key]) if any(counts.values()) else None
    return {'users': stage['users'], 'kind': kind, **counts}


def run_generator(args: argparse.Namespace, url: str, stages: list[int]) -> dict[str, Any]:
    name = 'codereferee-load-' + uuid.uuid4().hex[:8]
    manifest = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {
        'name': name, 'namespace': args.namespace,
        'labels': {'app.kubernetes.io/name': 'codereferee-load'}}, 'spec': {
        'automountServiceAccountToken': False, 'restartPolicy': 'Never',
        'containers': [{'name': 'load', 'image': 'python:3.12-alpine',
            'command': ['python', '-u', '-c', GENERATOR_CODE],
            'env': [{'name': 'TARGET_URL', 'value': url},
                    {'name': 'STAGES', 'value': ','.join(str(s) for s in stages)},
                    {'name': 'STAGE_SECONDS', 'value': str(args.stage_seconds)},
                    {'name': 'THINK_SECONDS', 'value': str(args.think_seconds)},
                    {'name': 'REQUEST_TIMEOUT', 'value': str(args.load_timeout_seconds)}],
            'resources': GENERATOR_RESOURCES}]}}
    limit = len(stages) * (args.stage_seconds + args.load_timeout_seconds + args.think_seconds + 5) + 120
    try:
        kubectl(args.namespace, 'apply', '-f', '-', input_text=json.dumps(manifest))
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            phase = kubectl(args.namespace, 'get', 'pod', name, '-o', 'jsonpath={.status.phase}').stdout.strip()
            if phase in ('Succeeded', 'Failed'):
                break
            time.sleep(2)
        else:
            raise RuntimeError(f'Load generator did not finish within {limit}s.')
        logs = kubectl(args.namespace, 'logs', name).stdout
        if phase != 'Succeeded':
            raise RuntimeError('Load generator failed: ' + logs[-500:])
        return json.loads(logs.strip().splitlines()[-1])
    finally:
        subprocess.run(kubectl_command('-n', args.namespace, 'delete', 'pod', name, '--ignore-not-found=true',
                                       '--wait=false'), capture_output=True, text=True,
                       env=kubectl_environment(), timeout=30)


def restart_count(namespace: str, selector: str) -> int | None:
    try:
        pods = json.loads(kubectl(namespace, 'get', 'pods', '-l', selector, '-o', 'json').stdout)
    except (RuntimeError, ValueError):
        return None
    return sum(status.get('restartCount', 0) for pod in pods.get('items', [])
               for status in pod.get('status', {}).get('containerStatuses', []))


def kubectl(namespace: str, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(kubectl_command('-n', namespace, *args), input=input_text, text=True,
                               capture_output=True, env=kubectl_environment(), timeout=130)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or 'kubectl failed')
    return completed


def parse_stages(value: str) -> list[int]:
    stages = [int(v) for v in value.split(',') if v.strip()]
    if not stages or any(not 1 <= s <= 500 for s in stages) or stages != sorted(set(stages)):
        raise SystemExit('--stages must be increasing user counts between 1 and 500.')
    return stages


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Ramp concurrent users against the deployed service.')
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--deployment', required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--service-port', type=int, required=True)
    parser.add_argument('--label-selector', required=True)
    parser.add_argument('--probe-path', default='/')
    parser.add_argument('--stages', default='10,50,100,200')
    parser.add_argument('--target-users', type=int, default=None)
    parser.add_argument('--stage-seconds', type=int, default=20)
    parser.add_argument('--think-seconds', type=float, default=1.0)
    parser.add_argument('--load-timeout-seconds', type=float, default=5.0)
    parser.add_argument('--baseline-probes', type=int, default=5)
    parser.add_argument('--recovery-probes', type=int, default=5)
    parser.add_argument('--request-timeout-seconds', type=float, default=2.0)
    parser.add_argument('--recovery-timeout-seconds', type=int, default=120)
    parser.add_argument('--local-port', type=int, default=18082)
    return parser.parse_args()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


if __name__ == '__main__':
    raise SystemExit(main())
