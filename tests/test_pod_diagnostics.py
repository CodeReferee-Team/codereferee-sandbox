import json
import argparse
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pod_diagnostics as diagnostics
import deploy_repository as deploy


def owner(kind, uid):
    return [{'kind': kind, 'uid': uid, 'controller': True}]


class PodDiagnosticsTests(unittest.TestCase):
    def collect(self, waiting='CrashLoopBackOff', previous=True, include_text=True):
        deployment = {'metadata': {'namespace': 'codereferee-test', 'uid': 'deployment-uid'}}
        rs = {'metadata': {'namespace': 'codereferee-test', 'uid': 'rs-uid',
                          'ownerReferences': owner('Deployment', 'deployment-uid')}}
        pod = {'metadata': {'namespace': 'codereferee-test', 'uid': 'pod-uid', 'name': 'api-pod',
                           'ownerReferences': owner('ReplicaSet', 'rs-uid')},
               'status': {'phase': 'Running', 'containerStatuses': [{'name': 'api', 'ready': False,
                   'restartCount': 3, 'state': {'waiting': {'reason': waiting}},
                   'lastState': {'terminated': {'reason': 'Error', 'exitCode': 1}}}]}}
        other = {'metadata': {'namespace': 'codereferee-test', 'uid': 'other', 'name': 'foreign-pod',
                              'ownerReferences': owner('ReplicaSet', 'foreign-rs')}}
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            args = command[1:]
            if args[:2] == ['get', 'deployment']:
                output = deployment
            elif args[:2] == ['get', 'replicasets']:
                output = {'items': [rs]}
            elif args[:2] == ['get', 'pods']:
                output = {'items': [pod, other]}
            elif args[:2] == ['get', 'events']:
                output = {'items': [{'involvedObject': {'uid': 'pod-uid'}, 'reason': 'Unhealthy',
                    'message': 'Readiness probe failed: HTTP probe failed with statuscode: 500', 'count': 2},
                    {'involvedObject': {'uid': 'foreign'}, 'message': 'must not be forwarded'}]}
            else:
                if '--previous' in args and not previous:
                    return subprocess.CompletedProcess(command, 1, '', 'no previous container')
                return subprocess.CompletedProcess(command, 0,
                    '\n'.join(['line'] * 60 + ["KeyError: DATABASE_URL", 'password=private-value']), '')
            return subprocess.CompletedProcess(command, 0, json.dumps(output), '')

        with patch.object(diagnostics, 'kubectl_command', side_effect=lambda *args: ['kubectl', *args]), \
             patch.object(diagnostics.subprocess, 'run', side_effect=run):
            result = diagnostics.collect_pod_diagnostics('codereferee-test', 'api', include_text=include_text)
        return result, commands

    def test_crashloop_logs_events_and_owned_scope(self):
        report, commands = self.collect()
        self.assertEqual(len(report['pods']), 1)
        pod = report['pods'][0]
        self.assertEqual(pod['containers'][0]['waiting_reason'], 'CrashLoopBackOff')
        self.assertEqual(pod['containers'][0]['last_terminated']['exit_code'], 1)
        self.assertEqual(pod['containers'][0]['logs_source'], 'previous')
        self.assertEqual(len(pod['logs_tail'].splitlines()), 50)
        self.assertNotIn('private-value', json.dumps(report))
        self.assertEqual(pod['events'][0]['reason'], 'Unhealthy')
        self.assertEqual(len(pod['events']), 1)
        self.assertTrue(all('codereferee-test' in command for command in commands))
        self.assertFalse(any('foreign-pod' in command for command in commands))

    def test_previous_logs_fall_back_to_current(self):
        report, _ = self.collect(previous=False)
        self.assertEqual(report['pods'][0]['containers'][0]['logs_source'], 'current')

    def test_unresolved_secret_withholds_free_text_without_querying_secrets(self):
        report, commands = self.collect(include_text=False)
        self.assertIn('secret_redaction_unresolved_text_withheld', report['collection_errors'])
        self.assertEqual(report['pods'][0]['logs_tail'], '')
        self.assertEqual(report['pods'][0]['events'][0]['message'], '[WITHHELD]')
        self.assertEqual(report['pods'][0]['events'][0]['reason'], 'Unhealthy')
        self.assertFalse(any('logs' in command or 'secret' in command or 'secrets' in command for command in commands))

    def test_image_pull_reason_preserved_without_invented_diagnosis(self):
        report, _ = self.collect(waiting='ImagePullBackOff')
        self.assertEqual(report['pods'][0]['containers'][0]['waiting_reason'], 'ImagePullBackOff')

    @patch.object(diagnostics.subprocess, 'run', side_effect=subprocess.TimeoutExpired('kubectl', 1))
    def test_collection_failure_is_partial_not_an_exception(self, run):
        report = diagnostics.collect_pod_diagnostics('codereferee-test', 'api')
        self.assertEqual(report['pods'], [])
        self.assertIn('deployment_ownership_unverified', report['collection_errors'])

    def test_redacts_known_values_credentials_and_authorization(self):
        text = 'postgresql://user:password@host/db authorization: Bearer abcd api_key="secret123" opaque-secret'
        masked = diagnostics.redact(text, ('opaque-secret',))
        for secret in ('user:password', 'abcd', 'secret123', 'opaque-secret'):
            self.assertNotIn(secret, masked)

    def test_rollout_failure_collects_before_cleanup_and_preserves_original_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'template.yaml').write_text('kind: Deployment\nspec: {}', encoding='utf-8')
            args = argparse.Namespace(request_id='diagnostic-test', repository_url='https://github.com/example/app',
                branch=None, commit_sha=None, profile='test', patch_file=None, rollout_timeout_seconds=1)
            profile = {'manifestTemplate': 'template.yaml', 'target': {'deployment': 'api', 'servicePort': 8000}}
            order = []
            def clone(url, branch, destination):
                destination.mkdir()
                (destination / 'Dockerfile').write_text('FROM python:3.12-slim', encoding='utf-8')
            def collect(*args, **kwargs):
                order.append('collect')
                return {'pods': [{'name': 'failed-pod'}]}
            def cleanup(*args):
                order.append('cleanup')
                return True
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'RUNTIME_ROOT', root / '.runtime'), \
                 patch.object(deploy, 'parse_args', return_value=args), patch.object(deploy, 'clone', side_effect=clone), \
                 patch.object(deploy, 'verify_repository', return_value={}), \
                 patch.object(deploy, 'resolve_plan', return_value={'source': 'test', 'profile': 'test'}), \
                 patch.object(deploy, 'load_profile', return_value=profile), \
                 patch.object(deploy, 'run', return_value=subprocess.CompletedProcess([], 0, 'a' * 40, '')), \
                 patch.object(deploy, 'load_image_into_cluster'), patch.object(deploy, 'apply'), \
                 patch.object(deploy, 'wait_rollout', side_effect=RuntimeError('timed out waiting for the condition')), \
                 patch.object(deploy, 'collect_pod_diagnostics', side_effect=collect), \
                 patch.object(deploy, 'delete_namespace', side_effect=cleanup), \
                 patch.object(deploy, 'cleanup_request_image', return_value={}):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(deploy.main(), 1)
            report = json.loads(output.getvalue())['result']
            self.assertEqual(order, ['collect', 'cleanup'])
            self.assertEqual(report['exitCode'], 1)
            self.assertIn('timed out waiting', report['stderr'])
            self.assertEqual(report['sandboxReport']['pod_diagnostics']['pods'][0]['name'], 'failed-pod')


if __name__ == '__main__':
    unittest.main()
