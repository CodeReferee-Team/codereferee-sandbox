"""Verify live submission, receiver receipt, persisted evidence, and request cleanup.

Run the real AI worker separately. Does not create a synthetic verdict or call an LLM.
"""
import argparse
import json
import math
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--submission-url', default='http://127.0.0.1:5174')
    parser.add_argument('--receiver-url', default='http://127.0.0.1:19091')
    parser.add_argument('--repository-url', default='https://github.com/docker/getting-started-app.git')
    parser.add_argument('--branch', default='main')
    parser.add_argument('--profile')
    parser.add_argument('--mode', default='litmus_pod_delete')
    parser.add_argument('--timeout-seconds', type=int, default=900)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--require-pod-replacement', action='store_true')
    parser.add_argument('--verify-existing', help='Verify a captured final result without submitting again.')
    args = parser.parse_args()
    opener = build_opener(ProxyHandler({}))
    payload = {'repository_url': args.repository_url, 'branch': args.branch, 'chaos_mode': args.mode}
    if args.profile:
        payload['deployment_profile'] = args.profile
    request = Request(args.submission_url.rstrip('/') + '/api/validations/repository',
                      data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    if args.verify_existing:
        captured = json.loads(Path(args.verify_existing).read_text(encoding='utf-8'))
        identifier = str(UUID(captured['taskId']))
    else:
        with opener.open(request, timeout=15) as response:
            identifier = str(UUID(json.load(response)['requestId']))
    print('requestId=' + identifier, flush=True)
    deadline = time.monotonic() + args.timeout_seconds
    last = None
    while time.monotonic() < deadline:
        if args.verify_existing:
            status = captured
        else:
            with opener.open(args.submission_url.rstrip('/') + '/api/validations/' + identifier, timeout=15) as response:
                status = json.load(response)
        if status['currentAgent'] != last:
            last = status['currentAgent']
            print('step=' + last, flush=True)
        if last not in ('PASSED', 'FAILED', 'ERROR'):
            time.sleep(3)
            continue
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
        execution = (status.get('aiReports') or {}).get('execution_result') or {}
        source = execution.get('source') or {}
        observation = source.get('metrics_observation') or {}
        cleanup = source.get('artifact_cleanup') or {}
        bindings = observation.get('pod_bindings') or []
        checks = {'chaos_observed': execution.get('observation_status') == 'observed' and execution.get('exit_code') == 0,
                  'metrics_received': observation.get('status') == 'received' and observation.get('receipt_confirmed') is True,
                  'observation_removed': observation.get('cleanup', {}).get('removed') is True,
                  'namespace_removed': cleanup.get('namespace_removed') is True,
                  'images_removed': cleanup.get('host_removed') is True and bool(cleanup.get('kind_nodes'))
                      and all(item.get('removed') for item in cleanup['kind_nodes']),
                  'workspace_removed': source.get('workspace_cleanup', {}).get('removed') is True}
        if args.require_pod_replacement:
            checks['replacement_bound'] = len({item['uid'] for item in bindings}) >= 2
        selector = '{codereferee_request_id=' + json.dumps(identifier) + ',namespace=' + json.dumps(observation.get('namespace', '')) + '}'
        for name in ('container_cpu_usage_seconds_total', 'container_memory_working_set_bytes'):
            # An instant at the end can legitimately fall in a scrape gap.
            # Check actual retained samples across the recorded interval.
            ended = observation.get('ended_at', time.time())
            window = max(1, math.ceil(ended - observation.get('started_at', ended)))
            params = {'query': f'sum(count_over_time({name}{selector}[{window}s]))', 'time': ended}
            with opener.open(args.receiver_url.rstrip('/') + '/api/v1/query?' + urlencode(params), timeout=10) as response:
                values = json.load(response).get('data', {}).get('result', [])
            checks[name + '_retained'] = bool(values) and float(values[0]['value'][1]) > 0
        sql = ("SELECT json_build_object('status',current_agent,'metrics',"
               "ai_reports->'execution_result'->'source'->'metrics_observation') FROM task_status WHERE task_id='" + identifier + "'")
        stored = {}
        for attempt in range(5):
            db = subprocess.run(['docker', 'exec', 'codereferee-db', 'psql', '-U', 'postgres', '-d', 'codereferee', '-At', '-c', sql],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
            if db.returncode:
                raise RuntimeError('PostgreSQL evidence read failed.')
            stored = json.loads(db.stdout.strip() or '{}')
            if stored.get('status') == last:
                break
            time.sleep(1)
        checks['postgres_persisted'] = stored.get('status') == last and (stored.get('metrics') or {}).get('status') == 'received'
        print(json.dumps({'taskId': identifier, 'backend_status': last, 'checks': checks,
                          'pod_uids': [item['uid'] for item in bindings]}, ensure_ascii=False), flush=True)
        # Judge failure is distinct from a failed transport/observation check.
        return 0 if all(checks.values()) else 1
    raise TimeoutError('No terminal Backend result for ' + identifier)


if __name__ == '__main__':
    raise SystemExit(main())
