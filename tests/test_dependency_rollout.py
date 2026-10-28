import argparse
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import deploy_repository as deploy
from execution_plan import render_plan


PLAN = {'source': 'repository_configuration', 'port': 8000, 'healthPath': '/health', 'replicas': 1,
        'resources': {'limits': {'cpu': '1000m', 'memory': '512Mi'}, 'requests': {'cpu': '100m', 'memory': '128Mi'}},
        'env': {}, 'dockerfile': 'Dockerfile', 'buildContext': '.',
        'dependencies': {'orders-db': {'type': 'postgres', 'probePath': '/api/orders'},
                         'session-cache': {'type': 'redis'}}}


class DependencyRolloutTest(unittest.TestCase):
    """의존성 Pod가 Ready가 되기 전에 실험이 시작되면 DB 시나리오가 'No Ready Pod'로 깨진다."""

    def run_deploy(self, wait_side_effect=None):
        rendered, _ = render_plan(PLAN, 'codereferee-dep-test', 'codereferee/dep-test:abc')
        waited = []

        def wait(namespace, deployment, timeout):
            waited.append(deployment)
            if wait_side_effect:
                wait_side_effect(deployment)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(request_id='dep-test', repository_url='https://github.com/example/app',
                branch=None, commit_sha=None, profile=None, patch_file=None, rollout_timeout_seconds=1)

            def clone(url, branch, destination):
                destination.mkdir()
                (destination / 'Dockerfile').write_text('FROM python:3.12-slim', encoding='utf-8')
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'RUNTIME_ROOT', root / '.runtime'), \
                 patch.object(deploy, 'parse_args', return_value=args), patch.object(deploy, 'clone', side_effect=clone), \
                 patch.object(deploy, 'verify_repository', return_value={}), \
                 patch.object(deploy, 'resolve_plan', return_value=dict(PLAN)), \
                 patch.object(deploy, 'render_plan', return_value=render_plan(PLAN, 'codereferee-dep-test', 'img')), \
                 patch.object(deploy, 'run', return_value=subprocess.CompletedProcess([], 0, 'a' * 40, '')), \
                 patch.object(deploy, 'load_image_into_cluster'), patch.object(deploy, 'apply'), \
                 patch.object(deploy, 'wait_rollout', side_effect=wait), \
                 patch.object(deploy, 'collect_pod_diagnostics', return_value={'pods': []}), \
                 patch.object(deploy, 'delete_namespace', return_value=True), \
                 patch.object(deploy, 'cleanup_request_image', return_value={}):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = deploy.main()
        return code, json.loads(output.getvalue()), waited

    def test_waits_for_every_declared_dependency(self):
        code, _, waited = self.run_deploy()
        self.assertEqual(code, 0)
        self.assertEqual(waited[0], 'repository-api')
        self.assertEqual(sorted(waited[1:]), ['orders-db', 'session-cache'])

    def test_dependency_rollout_failure_is_infrastructure_not_code_defect(self):
        def fail_db(deployment):
            if deployment == 'orders-db':
                raise RuntimeError('deployment "orders-db" exceeded its progress deadline')
        code, payload, _ = self.run_deploy(fail_db)
        result = payload['result']
        self.assertEqual(code, 1)
        self.assertEqual(result['observationStatus'], 'infrastructure_error')
        self.assertIsNone(result['exitCode'])
        self.assertEqual(result['sandboxReport']['failed_step'], 'dependencies')

    def test_execution_timeout_is_not_reported_as_a_user_defect(self):
        def timeout(_deployment):
            raise subprocess.TimeoutExpired(['kubectl'], 1)
        _, payload, _ = self.run_deploy(timeout)
        result = payload['result']
        self.assertEqual(result['observationStatus'], 'infrastructure_error')
        self.assertEqual(result['infraError'], 'sandbox_execution_timeout')
        self.assertIsNone(result['exitCode'])
        self.assertTrue(result['timedOut'])
        self.assertEqual(result['validationSupport'], 'unsupported_within_budget')
        self.assertEqual(result['sandboxReport']['execution_limit']['stage'], 'run')


if __name__ == '__main__':
    unittest.main()
