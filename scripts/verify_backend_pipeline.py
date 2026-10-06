"""Submit via the Frontend proxy and verify the real Redis/AI/Sandbox return path."""
import argparse
import json
import sys
import subprocess
from uuid import UUID
import time
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.scenarios import resolve_scenarios


def verify_postgres(container: str, task_id: str, step: str, scenario_count: int) -> bool:
    identifier = str(UUID(task_id))
    query = ("SELECT json_build_object('step', current_agent, 'observed', "
             "ai_reports->'execution_result'->>'observation_status', 'exit_code', "
             "ai_reports->'execution_result'->>'exit_code', 'scenarios', "
             "jsonb_array_length(ai_reports->'execution_result'->'chaos_observation'->'scenarios')) "
             f"FROM task_status WHERE task_id = '{identifier}'")
    # Cache may become terminal just before the DB upsert. Wait briefly for persistence.
    for attempt in range(5):
        result = subprocess.run(['docker', 'exec', container, 'psql', '-U', 'postgres',
            '-d', 'codereferee', '-tA', '-c', query], text=True, capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError('PostgreSQL persistence check failed: ' + result.stderr.strip())
        stored = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
        if (stored.get('step') == step and stored.get('observed') == 'observed'
                and stored.get('exit_code') == '0' and (stored.get('scenarios') or 0) == scenario_count):
            return True
        time.sleep(1)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend-url', default='http://127.0.0.1:5173')
    parser.add_argument('--repository-url', default='https://github.com/phdcoco/QuickByte_Demo.git')
    parser.add_argument('--branch', default='main')
    parser.add_argument('--mode', default='suite_custom__db__redis')
    parser.add_argument('--profile', default='quickbyte-demo')
    parser.add_argument('--timeout-seconds', type=int, default=1200)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verify-postgres', action='store_true')
    parser.add_argument('--postgres-container', default='codereferee-db')
    args = parser.parse_args()
    planned = resolve_scenarios(args.mode)
    opener = build_opener(ProxyHandler({}))
    payload = {'repository_url': args.repository_url, 'branch': args.branch,
               'chaos_mode': args.mode, 'deployment_profile': args.profile}
    request = Request(args.frontend_url + '/api/validations/repository',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    with opener.open(request, timeout=20) as response:
        request_id = json.loads(response.read())['requestId']
    print('requestId=' + request_id, flush=True)
    deadline = time.monotonic() + args.timeout_seconds
    last_step = None
    while time.monotonic() < deadline:
        with opener.open(args.frontend_url + '/api/validations/' + request_id, timeout=10) as response:
            status = json.loads(response.read())
        step = status['currentAgent']
        if step != last_step:
            print('step=' + step, flush=True)
            last_step = step
        if step in {'PASSED', 'FAILED', 'ERROR'}:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            execution = (status.get('aiReports') or {}).get('execution_result') or {}
            observation = execution.get('chaos_observation') or {}
            print(json.dumps({'taskId': request_id, 'status': step,
                'observationStatus': execution.get('observation_status'),
                'scenarios': [{'mode': r.get('chaosMode'), 'status': r.get('observationStatus'),
                    'exitCode': r.get('exitCode'), 'metrics': r.get('metrics')}
                    for r in observation.get('scenarios', [])]}, indent=2), flush=True)
            scenarios = observation.get('scenarios', [])
            complete = ([r.get('chaosMode') for r in scenarios] == planned
                        if args.mode.startswith('suite_') else len(planned) == 1)
            cleanup = (execution.get('source') or {}).get('artifact_cleanup') or {}
            verified = (step != 'ERROR' and execution.get('observation_status') == 'observed'
                        and execution.get('exit_code') == 0 and complete
                        and all(r.get('observationStatus') == 'observed' and r.get('exitCode') == 0
                                for r in scenarios)
                        and cleanup.get('namespace_removed') is True
                        and cleanup.get('host_removed') is True
                        and bool(cleanup.get('kind_nodes'))
                        and all(node.get('removed') is True for node in cleanup['kind_nodes'])
                        and not cleanup.get('errors'))
            print('pipeline_verified=' + str(verified), flush=True)
            if args.verify_postgres:
                persisted = verify_postgres(args.postgres_container, request_id, step, len(scenarios))
                print('postgres_verified=' + str(persisted), flush=True)
                verified = verified and persisted
            # A legitimate Judge FAILED verdict does not itself mean transport
            # failed; require actual completed observations and artifact cleanup.
            return 0 if verified else 1
        time.sleep(3)
    raise TimeoutError('Backend did not produce a terminal result: ' + request_id)


if __name__ == '__main__':
    raise SystemExit(main())
