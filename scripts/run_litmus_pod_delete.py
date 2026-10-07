#!/usr/bin/env python3
"""Run the Litmus Pod Delete fault for a deployed repository target.

The target is supplied by the repository executor; this script contains no
repository-specific namespace, image, or label values.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collect_baseline import kubectl_command, kubectl_environment
from in_cluster_probe import InClusterProbe
from run_pod_kill_experiment import get_target_configuration, get_ready_pod, metrics_from_probes, probe_once


# The Sandbox bootstrap installs only the permissions declared by the Pod
# Delete and Container Kill faults.  A namespaced RoleBinding limits them to
# each request-scoped target namespace.
ROLE = "codereferee-litmus-runner"
SCENARIOS = ('pod_delete', 'container_kill', 'pod_cpu_hog', 'pod_network_latency', 'pod_network_loss', 'pod_memory_hog', 'pod_memory_oom')


def fault_environment(scenario: str, container: str, duration: int, memory_mb: int = 32) -> dict[str, str]:
    if scenario not in SCENARIOS or not 1 <= duration <= 60:
        raise ValueError('Unsupported fault or duration outside 1..60 seconds.')
    env = {'TOTAL_CHAOS_DURATION': str(duration), 'CHAOS_INTERVAL': '60',
           'FORCE': 'true', 'TARGET_CONTAINER': container, 'PODS_AFFECTED_PERC': '100',
           'CONTAINER_RUNTIME': os.getenv('CODEREFEREE_CONTAINER_RUNTIME', 'containerd'),
           'SOCKET_PATH': os.getenv('CODEREFEREE_RUNTIME_SOCKET', '/run/containerd/containerd.sock')}
    if scenario == 'pod_cpu_hog':
        env.update(CPU_CORES='1', CPU_LOAD='80')
    if scenario.startswith('pod_network_'):
        env.update(NETWORK_INTERFACE='eth0', NETWORK_LATENCY='300', JITTER='0', NETWORK_PACKET_LOSS_PERCENTAGE='50')
    if scenario.startswith('pod_memory_'):
        env.update(MEMORY_CONSUMPTION=str(memory_mb), NUMBER_OF_WORKERS='1')
    return env


def memory_megabytes(value: str) -> int:
    match = re.fullmatch(r'(\d+)(Ki|Mi|Gi)?', value)
    if not match:
        raise ValueError('Memory faults require a declared Ki/Mi/Gi or byte memory limit.')
    scale = {None: 1, 'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3}[match[2]]
    return int(match[1]) * scale // (1024**2)


def classify_result(result: dict[str, Any]) -> str:
    if result.get('verdict') == 'Pass':
        return 'observed'
    status = result.get('raw', {}).get('experimentStatus', {})
    code = str((status.get('errorOutput') or {}).get('errorCode', ''))
    # Failed runtime/helper/setup must never be labelled as a user resilience failure.
    targets = result.get('raw', {}).get('history', {}).get('targets', [])
    injected = any(t.get('chaosStatus') in {'injected', 'reverted'} for t in targets)
    if code in {'STATUS_CHECKS_ERROR', 'PROBE_ERROR'} and injected:
        return 'observed'
    return 'infrastructure_error'


def measured_recovery_seconds(probes: list[dict[str, Any]]) -> float | None:
    """Measure the HTTP outage, excluding runner scheduling/image pull overhead.

    Zero means no failed request was observed, not that no degradation occurred.
    Raw probes and experiment duration are retained so callers can re-evaluate.
    """
    failed = [i for i, probe in enumerate(probes) if not probe['success']]
    if not failed:
        return 0.0
    after = probes[failed[-1] + 1:]
    if len(after) < 5 or not all(p['success'] for p in after[-5:]):
        return None
    start = datetime.fromisoformat(probes[failed[0]]['at'].replace('Z', '+00:00'))
    end = datetime.fromisoformat(after[0]['at'].replace('Z', '+00:00'))
    return round((end - start).total_seconds(), 2)


def oom_during_experiment(terminations: list[dict[str, Any]], started_at: str,
                          before: int, after: int) -> bool:
    if after <= before:
        return False
    start = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
    return any(t.get('reason') == 'OOMKilled' and t.get('finishedAt') and
               datetime.fromisoformat(t['finishedAt'].replace('Z', '+00:00')) >= start for t in terminations)


def main() -> int:
    args = parse_args()
    target = load_target(args)
    required = ("namespace", "deployment", "labelSelector")
    missing = [name for name in required if not target.get(name)]
    if missing:
        raise SystemExit(f"target file missing: {', '.join(missing)}")
    prefix = 'codereferee-' + args.scenario.replace('_', '-') + '-'
    suffix = f'-{int(time.time())}'
    name = prefix + safe_name(target.get('name') or target['deployment'])[:63-len(prefix)-len(suffix)] + suffix
    source_pod = get_ready_pod(target["namespace"], target["labelSelector"], target.get("service") or target["deployment"])
    pod_json = subprocess.run(kubectl_command('-n', target['namespace'], 'get', 'pod', source_pod['name'], '-o', 'json'),
                              text=True, capture_output=True, check=True, env=kubectl_environment())
    containers = json.loads(pod_json.stdout)['spec']['containers']
    target_container = args.target_container or containers[0]['name']
    memory_mb = 32
    if args.scenario.startswith('pod_memory_'):
        selected = next(c for c in containers if c['name'] == target_container)
        limit = memory_megabytes(selected.get('resources', {}).get('limits', {}).get('memory', ''))
        if limit < 32 or limit > 4096:
            raise RuntimeError('Memory faults require a container memory limit between 32Mi and 4Gi.')
        memory_mb = max(1, int(limit * (1.25 if args.scenario == 'pod_memory_oom' else 0.2)))
    target_configuration = get_target_configuration(target["namespace"], target["deployment"])
    with InClusterProbe(target['namespace'], target['service'], int(target['servicePort']),
                        timeout=args.request_timeout_seconds, local_port=args.local_port,
                        path=target.get('probePath', args.probe_path)) as observer:
        baseline = {'probes': observer.collect(args.baseline_probes)}
        if not all(probe['success'] for probe in baseline['probes']):
            raise RuntimeError('Target baseline is unhealthy; fault injection was skipped.')
        fault_started_monotonic = time.monotonic()
        fault_started_at = now_utc()
        apply(target["namespace"], name, target["deployment"], target["labelSelector"], args.scenario, target_container, args.fault_seconds, memory_mb)
        result, recovery_probes = wait_for_result(target["namespace"], name, args.timeout_seconds, args.local_port,
                                                   args.request_timeout_seconds, observer=observer)
        recovery_probes.extend(observer.collect(5))
        recovered = result.get('verdict') == 'Pass' and all(p['success'] for p in recovery_probes[-5:])
        recovered_at = now_utc() if recovered else None
    observation_status = classify_result(result)
    try:
        replacement_pod = get_ready_pod(target["namespace"], target["labelSelector"], target.get("service") or target["deployment"])
    except RuntimeError:
        replacement_pod = {}
    final_status = subprocess.run(kubectl_command('-n', target['namespace'], 'get', 'pods', '-l', target['labelSelector'], '-o', 'json'),
        text=True, capture_output=True, env=kubectl_environment())
    terminations = []
    if final_status.returncode == 0:
        for pod in json.loads(final_status.stdout).get('items', []):
            for status in pod.get('status', {}).get('containerStatuses', []):
                if status['name'] == target_container:
                    termination = status.get('lastState', {}).get('terminated')
                    if termination:
                        terminations.append({k: termination.get(k) for k in ('reason', 'exitCode', 'startedAt', 'finishedAt')})
    output = {"schemaVersion": "chaos-v1", "scenario": args.scenario, "observationStatus": observation_status,
              'exitCode': (0 if recovered else 1) if observation_status == 'observed' else None,
              'infraError': 'litmus_fault_execution_failed' if observation_status == 'infrastructure_error' else None,
              'probeTransport': 'in_cluster_http',
              "target": target, "replicas": target_configuration["replicas"], "chaosEngine": name, "chaosResult": result,
              "chaos_observation": {"type": args.scenario, "kill_method": f"litmus_{args.scenario}", "started_at": fault_started_at,
                                    "recovered": recovered,
                                    "recovered_at": recovered_at, "recovery_seconds": measured_recovery_seconds(recovery_probes) if recovered else None,
                                    'recovery_measurement': 'first_failed_http_request_to_first_success_after_last_failure; 0 when no HTTP outage was sampled',
                                    'experiment_duration_seconds': round(time.monotonic() - fault_started_monotonic, 2),
                                    'fault_parameters': fault_environment(args.scenario, target_container, args.fault_seconds, memory_mb),
                                    'last_terminations': terminations,
                                    'oom_observed': oom_during_experiment(terminations, fault_started_at,
                                        source_pod.get('restart_count', 0), replacement_pod.get('restart_count', 0)) if args.scenario == 'pod_memory_oom' else None,
                                    'target_container': target_container,
                                    'target_restart_count': source_pod.get('restart_count'),
                                    'replacement_restart_count': replacement_pod.get('restart_count'),
                                    "target_pod_uid": source_pod.get("uid"),
                                    "replacement_pod_uid": replacement_pod.get("uid"),
                                    "target_configuration": target_configuration,
                                    "observation_window": {"baseline_probe_count": args.baseline_probes,
                                      "recovery_timeout_seconds": args.timeout_seconds,
                                      "error_rate_denominator": "all in-cluster HTTP requests during baseline, experiment, and recovery verification"},
                                    "abort_condition": {"triggered": result.get("verdict") == "Stopped",
                                      "reason": result.get("verdict") if result.get("verdict") != "Pass" else None}},
              "source": {"real_execution_observed": True, "target": target.get("deployment")}}
    if baseline is not None:
        probes = [*baseline["probes"], *recovery_probes]
        output["baseline"] = {"metrics": metrics_from_probes(baseline["probes"])}
        output["metrics"] = metrics_from_probes(probes)
        output['metrics']['restart_count'] = replacement_pod.get('restart_count')
        output['metrics']['error_rate_denominator'] = 'all in-cluster HTTP requests during baseline, experiment, and recovery verification'
        output['baseline']['metrics']['error_rate_denominator'] = 'baseline in-cluster HTTP requests only'
        output['probes'] = {'baseline': baseline['probes'], 'experiment': recovery_probes}
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if recovered else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run generic Litmus Pod Delete.")
    parser.add_argument("--target-file", type=Path)
    parser.add_argument("--namespace")
    parser.add_argument("--deployment")
    parser.add_argument("--label-selector")
    parser.add_argument("--service")
    parser.add_argument("--service-port", type=int)
    parser.add_argument("--baseline-probes", type=int, default=20)
    parser.add_argument("--request-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--local-port", type=int, default=18080)
    parser.add_argument('--probe-path', default='/')
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--output", type=Path, help="Optional JSON evidence output path.")
    parser.add_argument("--scenario", choices=SCENARIOS, default="pod_delete")
    parser.add_argument("--target-container")
    parser.add_argument('--fault-seconds', type=int, default=15)
    return parser.parse_args()


def load_target(args: argparse.Namespace) -> dict[str, Any]:
    if args.target_file:
        return json.loads(args.target_file.read_text(encoding="utf-8")).get("target", {})
    return {"namespace": args.namespace, "deployment": args.deployment, "labelSelector": args.label_selector,
            "service": args.service, "servicePort": args.service_port}


def apply(namespace: str, engine: str, deployment: str, selector: str, scenario: str, target_container: str, duration: int = 15, memory_mb: int = 32, overrides: dict[str, str] | None = None) -> None:
    experiment = 'pod-memory-hog' if scenario == 'pod_memory_oom' else scenario.replace("_", "-")
    copy_experiment(namespace, experiment)
    environment = fault_environment(scenario, target_container, duration, memory_mb)
    environment.update(overrides or {})
    env_yaml = '\n'.join(f'            - name: {key}\n              value: "{value}"' for key, value in environment.items())
    manifest = f'''apiVersion: v1
kind: ServiceAccount
metadata:
  name: litmus-admin
  namespace: {namespace}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: codereferee-litmus-admin
  namespace: {namespace}
subjects:
  - kind: ServiceAccount
    name: litmus-admin
    namespace: {namespace}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {ROLE}
---
apiVersion: litmuschaos.io/v1alpha1
kind: ChaosEngine
metadata:
  name: {engine}
  namespace: {namespace}
spec:
  appinfo:
    appns: {namespace}
    applabel: "{selector}"
    appkind: deployment
  jobCleanUpPolicy: delete
  engineState: active
  chaosServiceAccount: litmus-admin
  experiments:
    - name: {experiment}
      spec:
        components:
          env:
{env_yaml}
'''
    completed = subprocess.run(kubectl_command("apply", "-f", "-"), input=manifest, text=True, capture_output=True,
                               env=kubectl_environment())
    if completed.returncode:
        raise SystemExit(completed.stderr.strip() or completed.stdout.strip())


def copy_experiment(namespace: str, experiment_name: str) -> None:
    """Copy the official chart's namespaced Pod Delete definition to the target."""
    source = subprocess.run(kubectl_command("get", "chaosexperiment", experiment_name, "-n", "litmus", "-o", "json"),
                            text=True, capture_output=True, env=kubectl_environment(), check=True)
    experiment = json.loads(source.stdout)
    experiment.pop("status", None)
    metadata = experiment["metadata"]
    for field in ("creationTimestamp", "generation", "resourceVersion", "uid", "managedFields", "annotations"):
        metadata.pop(field, None)
    metadata["namespace"] = namespace
    copied = subprocess.run(kubectl_command("apply", "-f", "-"), input=json.dumps(experiment), text=True,
                            capture_output=True, env=kubectl_environment())
    if copied.returncode:
        raise SystemExit(copied.stderr.strip() or copied.stdout.strip())


def wait_for_result(namespace: str, engine: str, timeout: int, local_port: int | None,
                    request_timeout_seconds: float, *, observer=None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout
    probes: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        if local_port:
            probes.append(observer.probe() if observer else probe_once(local_port, request_timeout_seconds))
        result = subprocess.run(kubectl_command("get", "chaosresult", "-n", namespace, "-o", "json"), text=True,
                                capture_output=True, env=kubectl_environment())
        if result.returncode == 0:
            for item in json.loads(result.stdout).get("items", []):
                if item.get("metadata", {}).get("name", "").startswith(engine + "-"):
                    status = item.get("status", {})
                    verdict = status.get("experimentStatus", {}).get("verdict")
                    if verdict in {"Pass", "Fail", "Stopped"}:
                        return (
                            {"name": item["metadata"]["name"], "verdict": verdict,
                             "phase": status.get("experimentStatus", {}).get("phase"), "raw": status},
                            probes,
                        )
        time.sleep(2)
    raise SystemExit(f"Timed out waiting for Litmus result from {engine}")


def safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:40]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
