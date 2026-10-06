import subprocess
import unittest
from unittest.mock import patch

from app.scenarios import CUSTOM_V1, resolve_scenarios
from app.main import ChaosTarget, RepositoryValidationRequest, experiment_command, validate_repository


class ScenarioSuiteTests(unittest.TestCase):
    def test_all_runtime_modes_route_through_generic_litmus_runner(self):
        target = ChaosTarget(namespace='test', deployment='api', service='api',
                             servicePort=8080, labelSelector='app=api')
        for mode in ('litmus_container_kill', 'litmus_pod_network_latency',
                     'litmus_pod_network_loss', 'litmus_pod_cpu_hog', 'litmus_pod_memory_hog', 'litmus_pod_memory_oom'):
            request = RepositoryValidationRequest(repositoryUrl='https://github.com/test/repo', chaosMode=mode)
            command = experiment_command(request, target)
            self.assertIn('--scenario', command)
            self.assertIn(mode.removeprefix('litmus_'), command)
    def test_compact_custom_selection_matches_frontend_64_character_contract(self):
        self.assertEqual(resolve_scenarios('suite_custom__v1_fff'), list(CUSTOM_V1))
        self.assertEqual(resolve_scenarios('suite_custom__ck__cpu'),
                         ['litmus_container_kill', 'litmus_pod_cpu_hog'])
        with self.assertRaises(ValueError):
            resolve_scenarios('suite_custom__v1_1000')
    def test_suite_order_and_additional_modes_are_deduplicated(self):
        self.assertEqual(resolve_scenarios('suite_quick', ['litmus_pod_cpu_hog', 'litmus_container_kill']),
                         ['litmus_container_kill', 'litmus_pod_cpu_hog'])
        with self.assertRaises(ValueError):
            resolve_scenarios('suite_quick', ['unknown'])

    @patch('app.main.cleanup_namespace')
    @patch('app.main.subprocess.run')
    @patch('app.main.deploy_repository')
    def test_suite_deploys_once_and_stops_on_infrastructure_error(self, deploy, run, cleanup):
        import json
        deploy.return_value = {'target': {'namespace': 'test', 'deployment': 'api', 'service': 'api',
                               'servicePort': 8080, 'labelSelector': 'app=api'}}
        run.return_value = subprocess.CompletedProcess([], 1, json.dumps({
            'observationStatus': 'infrastructure_error', 'infraError': 'runtime_unavailable', 'exitCode': None}), '')
        request = RepositoryValidationRequest(repositoryUrl='https://github.com/test/repo',
            requestId='test', deploymentProfile='test', chaosMode='suite_standard')
        result = validate_repository(request)
        self.assertEqual(deploy.call_count, 1)
        self.assertEqual(run.call_count, 1)
        cleanup.assert_called_once_with('test')
        self.assertEqual(result['observationStatus'], 'infrastructure_error')
        self.assertEqual(result['chaos_observation']['not_executed'],
                         ['litmus_pod_cpu_hog', 'litmus_pod_network_latency'])
