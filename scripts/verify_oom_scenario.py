#!/usr/bin/env python3
"""Live, opt-in OOM regression in a dedicated namespace; never uses an existing app.

Run with the Sandbox Python, kubectl context configured, and --run-live. Evidence
is written under ignored .runtime/evidence. No AWS resources are provisioned.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

from collect_baseline import kubectl_command, kubectl_environment

ROOT = Path(__file__).resolve().parents[1]
APP = '''from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
import os
marker=Path('/state/started')
failed=os.environ.get('FAIL_AFTER_RESTART')=='1' and marker.exists()
marker.touch()
pad=bytearray(12*1024*1024)
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(503 if failed else 200);self.end_headers();self.wfile.write(b'fixture')
 def log_message(self,*args): pass
HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
'''


def fixture(namespace: str, replicas: int, fail: bool) -> dict:
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [
        {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}},
        {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'namespace': namespace, 'name': 'app'},
         'spec': {'replicas': replicas, 'selector': {'matchLabels': {'app': 'oom-review'}},
                  'template': {'metadata': {'labels': {'app': 'oom-review'}}, 'spec': {
                      'containers': [{'name': 'app', 'image': 'python:3.12-alpine', 'imagePullPolicy': 'IfNotPresent',
                          'command': ['python', '-u', '-c', APP],
                          'env': [{'name': 'FAIL_AFTER_RESTART', 'value': '1' if fail else '0'}],
                          'volumeMounts': [{'name': 'state', 'mountPath': '/state'}],
                          'resources': {'requests': {'cpu': '25m', 'memory': '32Mi'},
                                        'limits': {'cpu': '200m', 'memory': '64Mi'}},
                          'readinessProbe': {'tcpSocket': {'port': 8080}, 'periodSeconds': 1}}],
                      'volumes': [{'name': 'state', 'emptyDir': {}}]}}}},
        {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'namespace': namespace, 'name': 'app'},
         'spec': {'selector': {'app': 'oom-review'}, 'ports': [{'port': 8080, 'targetPort': 8080}]}}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true', required=True)
    parser.add_argument('--case', choices=['recovered', 'single-target', 'all-targets', 'unrecovered', 'memory-pressure', 'container-kill'], required=True)
    args = parser.parse_args()
    namespace = 'codereferee-oom-check-' + uuid.uuid4().hex[:8]
    replicas = 2 if args.case in ('single-target', 'all-targets') else 1
    scenario = {'memory-pressure': 'pod_memory_hog', 'container-kill': 'container_kill'}.get(args.case, 'pod_memory_oom')
    environment = dict(kubectl_environment(), PYTHONIOENCODING='utf-8')
    def kubectl(*parts, payload=None):
        return subprocess.run(kubectl_command(*parts), input=json.dumps(payload) if payload else None,
            capture_output=True, text=True, encoding='utf-8', check=True, env=environment, timeout=90)
    try:
        kubectl('apply', '-f', '-', payload=fixture(namespace, replicas, args.case == 'unrecovered'))
        kubectl('-n', namespace, 'rollout', 'status', 'deployment/app', '--timeout=75s')
        command = [sys.executable, str(ROOT/'scripts/run_litmus_pod_delete.py'), '--namespace', namespace,
            '--deployment', 'app', '--service', 'app', '--service-port', '8080', '--label-selector', 'app=oom-review',
            '--scenario', scenario, '--baseline-probes', '5', '--timeout-seconds',
            '45' if args.case == 'unrecovered' else '120', '--fault-seconds', '5']
        if args.case != 'all-targets': command += ['--pods-affected-count', '1']
        completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', env=environment, timeout=220)
        result = json.loads(completed.stdout)
        result['validation_case'] = args.case
        evidence = ROOT/'.runtime/evidence'/f'oom-{args.case}-{namespace[-8:]}.json'
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps(result, indent=2), encoding='utf-8')
        # Verify the Deployment and recovered containers retain their original limits.
        pods = json.loads(kubectl('-n', namespace, 'get', 'pods', '-l', 'app=oom-review', '-o', 'json').stdout)['items']
        limits = [p['spec']['containers'][0]['resources']['limits']['memory'] for p in pods]
        assert limits == ['64Mi'] * replicas, limits
        for pod in pods:
            actual_limit = kubectl('-n', namespace, 'exec', pod['metadata']['name'], '-c', 'app', '--',
                'python', '-c', "from pathlib import Path; print(Path('/sys/fs/cgroup/memory.max').read_text().strip())").stdout.strip()
            assert actual_limit == str(64*1024**2), actual_limit
        observation = result['chaos_observation']
        assert result['observationStatus'] == 'observed', result
        assert result['exitCode'] == (1 if args.case == 'unrecovered' else 0), result
        if scenario == 'pod_memory_oom':
            assert observation['oom_observed'] is True, result
            assert len(observation['target_oom_evidence']) == (2 if args.case == 'all-targets' else 1)
            assert all(r.get('restore') in ('restored', 'target_cgroup_removed')
                       for r in result['chaosResult']['helper_reports']), result
            if args.case == 'single-target':
                counts = sorted(p['status']['containerStatuses'][0]['restartCount'] for p in pods)
                assert counts == [0, 1], counts
        print(json.dumps({'case': args.case, 'observationStatus': result['observationStatus'],
            'exitCode': result['exitCode'], 'oom_observed': observation['oom_observed'],
            'recovered': observation['recovered'], 'recovery_seconds': observation['recovery_seconds'],
            'evidence': str(evidence)}, ensure_ascii=False), flush=True)
    finally:
        kubectl('delete', 'namespace', namespace, '--ignore-not-found', '--wait=true', '--timeout=75s')


if __name__ == '__main__': main()
