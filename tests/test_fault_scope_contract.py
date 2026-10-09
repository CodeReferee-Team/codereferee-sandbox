import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from app.main import ChaosTarget, RepositoryValidationRequest, experiment_command, validate_repository
from run_litmus_pod_delete import SCENARIOS, apply, fault_environment, scope_evidence, selected_pod_after


def pod(name, uid, restarts=0):
    return {'metadata': {'name': name, 'uid': uid}, 'status': {'containerStatuses': [
        {'name': 'api', 'restartCount': restarts, 'ready': True}]}}


class FaultScopeContractTests(unittest.TestCase):
    def request(self, **kwargs):
        return RepositoryValidationRequest(repositoryUrl='https://github.com/example/repo',
                                           chaosMode='litmus_container_kill', **kwargs)

    def target(self):
        return ChaosTarget(namespace='test', deployment='api', service='api', servicePort=8000, labelSelector='app=api')

    def test_http_camel_and_snake_alias_and_strict_bounds(self):
        self.assertIsNone(self.request().pods_affected_count)
        self.assertEqual(self.request(podsAffectedCount=1).pods_affected_count, 1)
        self.assertEqual(self.request(pods_affected_count=2).pods_affected_count, 2)
        for value in (0, -1, 17, True, '1', 1.0):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.request(podsAffectedCount=value)

    def test_legacy_command_omits_new_option_and_counted_command_passes_it(self):
        self.assertNotIn('--pods-affected-count', experiment_command(self.request(), self.target()))
        command = experiment_command(self.request(podsAffectedCount=1), self.target())
        self.assertEqual(command[command.index('--pods-affected-count') + 1], '1')

    def test_scope_is_rejected_before_deployment_for_unsupported_modes(self):
        for mode in (None, 'fixture', 'deployment_scale_down', 'rollout_restart', 'dependency_redis_outage'):
            request = RepositoryValidationRequest(repositoryUrl='https://github.com/example/repo',
                                                 chaosMode=mode, podsAffectedCount=1)
            with patch('app.main.deploy_repository') as deploy, self.assertRaises(HTTPException) as caught:
                validate_repository(request)
            self.assertEqual(caught.exception.status_code, 422)
            deploy.assert_not_called()

    def test_all_litmus_faults_keep_legacy_percent_and_support_exact_names(self):
        for scenario in SCENARIOS:
            legacy = fault_environment(scenario, 'api', 15)
            self.assertEqual(legacy['PODS_AFFECTED_PERC'], '100')
            self.assertNotIn('TARGET_PODS', legacy)
            scoped = fault_environment(scenario, 'api', 15, target_pods=['api-a'])
            self.assertEqual(scoped['TARGET_PODS'], 'api-a')
            self.assertEqual(scoped['PODS_AFFECTED_PERC'], '100')

    def test_empty_duplicate_or_unsafe_targets_cannot_widen_or_inject_yaml(self):
        for names in ([], ['api-a', 'api-a'], ['api-a\nvalue: injected'], ['api-a,api-b']):
            with self.subTest(names=names), self.assertRaises(ValueError):
                fault_environment('container_kill', 'api', 15, target_pods=names)

    @patch('run_litmus_pod_delete.copy_experiment')
    @patch('run_litmus_pod_delete.subprocess.run')
    def test_rendered_chaos_engine_has_authoritative_target_pods(self, run, copy):
        run.return_value = subprocess.CompletedProcess([], 0, '', '')
        apply('test', 'test-engine', 'api', 'app=api', 'container_kill', 'api', target_pods=['api-a'])
        manifest = run.call_args.kwargs['input']
        self.assertIn('name: TARGET_PODS\n              value: "api-a"', manifest)
        self.assertIn('name: PODS_AFFECTED_PERC\n              value: "100"', manifest)

    def test_history_distinguishes_exact_partial_unknown_and_unexpected_scope(self):
        selected = [pod('api-a', 'uid-a'), pod('api-b', 'uid-b')]
        def report(targets):
            return scope_evidence(2, selected, selected, selected, 'api', {'raw': {'history': {'targets': targets}}})
        exact = report([{'name': 'api-a', 'kind': 'pod', 'chaosStatus': 'targeted'},
                        {'name': 'api-b', 'kind': 'pod', 'chaosStatus': 'targeted'}])
        self.assertTrue(exact['reported_pod_scope_matches'])
        partial = report([{'name': 'api-a', 'kind': 'pod'}])
        self.assertFalse(partial['reported_pod_scope_matches'])
        self.assertEqual(partial['missing_reported_pods'], ['api-b'])
        parent = report([{'name': 'api', 'kind': 'deployment', 'chaosStatus': 'targeted'}])
        self.assertIsNone(parent['reported_pod_scope_matches'])
        wrong = report([{'name': 'api-other', 'kind': 'pod'}])
        self.assertEqual(wrong['unexpected_reported_pods'][0]['name'], 'api-other')

    def test_restarted_selected_uid_is_not_replaced_by_surviving_replica(self):
        before = [pod('api-a', 'uid-a'), pod('api-b', 'uid-b')]
        after = [before[1], pod('api-a', 'uid-a', 1)]
        result = selected_pod_after([before[0]], before, after, 'api')
        self.assertEqual((result['uid'], result['restart_count']), ('uid-a', 1))

    def test_deleted_target_uses_new_uid_not_unaffected_survivor(self):
        before = [pod('api-a', 'uid-a'), pod('api-b', 'uid-b')]
        after = [before[1], pod('api-c', 'uid-c')]
        self.assertEqual(selected_pod_after([before[0]], before, after, 'api')['uid'], 'uid-c')
        self.assertEqual(selected_pod_after([before[0]], before, [before[1]], 'api'), {})


if __name__ == '__main__':
    unittest.main()
