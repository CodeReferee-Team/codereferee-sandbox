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
        # 프론트가 보내는 12비트 선택은 그대로 해석되고, 부하(13번째)는 새 비트를 쓴다.
        self.assertEqual(resolve_scenarios('suite_custom__v1_fff'), list(CUSTOM_V1[:12]))
        self.assertEqual(resolve_scenarios('suite_custom__v1_1fff'), list(CUSTOM_V1))
        self.assertEqual(CUSTOM_V1[12], 'load_concurrent_users')
        self.assertEqual(resolve_scenarios('suite_custom__ck__cpu'),
                         ['litmus_container_kill', 'litmus_pod_cpu_hog'])
        with self.assertRaises(ValueError):
            resolve_scenarios('suite_custom__v1_2000')
    def test_standard_suites_measure_concurrent_users_before_injecting_faults(self):
        self.assertEqual(resolve_scenarios('suite_standard')[0], 'load_concurrent_users')
        self.assertEqual(resolve_scenarios('suite_deep')[0], 'load_concurrent_users')
        self.assertEqual(resolve_scenarios('suite_custom__load'), ['load_concurrent_users'])
        target = ChaosTarget(namespace='test', deployment='api', service='api', servicePort=8080,
                             labelSelector='app=api', probePath='/health')
        request = RepositoryValidationRequest(repositoryUrl='https://github.com/test/repo', chaosMode='load_concurrent_users')
        command = experiment_command(request, target)
        self.assertTrue(command[1].endswith('run_load_experiment.py'))
        self.assertEqual(command[command.index('--probe-path') + 1], '/health')
        self.assertNotIn('--target-users', command)
        targeted = request.model_copy(update={'target_users': 150})
        command = experiment_command(targeted, target)
        self.assertEqual(command[command.index('--target-users') + 1], '150')
        with self.assertRaises(ValueError):
            RepositoryValidationRequest(repositoryUrl='https://github.com/test/repo', targetUsers=501)

    def test_suite_order_and_additional_modes_are_deduplicated(self):
        self.assertEqual(resolve_scenarios('suite_quick', ['litmus_pod_cpu_hog', 'litmus_container_kill']),
                         ['litmus_container_kill', 'litmus_pod_cpu_hog'])
        with self.assertRaises(ValueError):
            resolve_scenarios('suite_quick', ['unknown'])

    # 묶음 동작만 보는 테스트라 표준 묶음 구성(2026-10-30 부하 추가)에 묶이지 않게 명시 선택을 쓴다.
    def run_suite(self, deploy, run, outcomes, mode='suite_custom__ck__cpu__lat', **extra):
        import json
        deploy.return_value = {'target': {'namespace': 'test', 'deployment': 'api', 'service': 'api',
                               'servicePort': 8080, 'labelSelector': 'app=api'}}
        run.side_effect = [o if isinstance(o, BaseException) else
                           subprocess.CompletedProcess([], 0 if o.get('exitCode') == 0 else 1, json.dumps(o), '')
                           for o in outcomes]
        request = RepositoryValidationRequest(repositoryUrl='https://github.com/test/repo',
            requestId='test', deploymentProfile='test', chaosMode=mode, **extra)
        return validate_repository(request)

    @staticmethod
    def observed(exit_code=0, recovery=1.0):
        return {'observationStatus': 'observed', 'exitCode': exit_code,
                'metrics': {'probe_count': 10, 'success_count': 9},
                'chaos_observation': {'recovered': exit_code == 0, 'recovery_seconds': recovery,
                                      'target_configuration': {'replicas': 1}},
                'baseline': {'metrics': {'availability': 1.0}}}

    @patch('app.main.cleanup_namespace')
    @patch('app.main.run_bounded')
    @patch('app.main.deploy_repository')
    def test_suite_deploys_once_and_reports_error_when_no_scenario_ran(self, deploy, run, cleanup):
        infra = {'observationStatus': 'infrastructure_error', 'infraError': 'runtime_unavailable',
                 'exitCode': None, 'stderr': 'netem missing'}
        result = self.run_suite(deploy, run, [infra, infra, infra])
        self.assertEqual(deploy.call_count, 1)
        self.assertEqual(run.call_count, 3)
        cleanup.assert_called_once_with('test')
        self.assertEqual(result['observationStatus'], 'infrastructure_error')
        self.assertEqual(result['stoppedAt'], 'scenario:litmus_container_kill')
        self.assertTrue(result['chaos_observation']['abort_condition']['triggered'])

    @patch('app.main.cleanup_namespace')
    @patch('app.main.run_bounded')
    @patch('app.main.deploy_repository')
    def test_suite_judges_scenarios_that_ran_and_names_the_one_that_stopped(self, deploy, run, cleanup):
        # M10: ck and cpu recovered, latency stopped on a missing netem module.
        netem = {'observationStatus': 'infrastructure_error', 'infraError': 'litmus_fault_execution_failed',
                 'exitCode': None, 'stderr': 'helper log\nSpecified qdisc kind is unknown'}
        result = self.run_suite(deploy, run, [self.observed(recovery=4.2), self.observed(recovery=0.0), netem])
        observation = result['chaos_observation']
        self.assertEqual(result['observationStatus'], 'observed')
        self.assertEqual(result['exitCode'], 0)
        self.assertTrue(observation['partial'])
        self.assertFalse(observation['abort_condition']['triggered'])
        self.assertEqual(observation['executed'], ['litmus_container_kill', 'litmus_pod_cpu_hog'])
        self.assertEqual(observation['stopped'][0]['chaosMode'], 'litmus_pod_network_latency')
        self.assertEqual(observation['stopped'][0]['reason'], 'Specified qdisc kind is unknown')
        self.assertEqual(result['metrics']['probe_count'], 20)

    @patch('app.main.cleanup_namespace')
    @patch('app.main.run_bounded')
    @patch('app.main.deploy_repository')
    def test_suite_stops_after_a_target_that_did_not_recover(self, deploy, run, cleanup):
        result = self.run_suite(deploy, run, [self.observed(exit_code=1)])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['exitCode'], 1)
        self.assertEqual(result['chaos_observation']['not_executed'],
                         ['litmus_pod_cpu_hog', 'litmus_pod_network_latency'])

    @patch('app.main.cleanup_namespace')
    @patch('app.main.run_bounded')
    @patch('app.main.deploy_repository')
    def test_suite_downtime_is_summed_for_the_error_budget(self, deploy, run, cleanup):
        # M09: two outages of about 37 s. The bound compares the longest, the budget spends both.
        result = self.run_suite(deploy, run, [self.observed(recovery=37.7), self.observed(recovery=35.5)],
                                mode='suite_custom__ck__pd')
        self.assertEqual(result['chaos_observation']['recovery_seconds'], 37.7)
        self.assertEqual(result['chaos_observation']['total_downtime_seconds'], 73.2)

    @patch('app.main.cleanup_namespace')
    @patch('app.main.run_bounded')
    @patch('app.main.deploy_repository')
    def test_scenario_timeout_in_a_suite_is_recorded_and_the_suite_goes_on(self, deploy, run, cleanup):
        from app.main import StageTimeout
        result = self.run_suite(deploy, run, [StageTimeout('scenario:litmus_container_kill', 300),
                                              self.observed(), self.observed()])
        stopped = result['chaos_observation']['stopped'][0]
        self.assertEqual(stopped['infraError'], 'sandbox_execution_timeout')
        self.assertTrue(stopped['timedOut'])
        self.assertEqual(result['observationStatus'], 'observed')


class StoppedReasonTests(unittest.TestCase):
    def test_litmus_helper_reason_is_preferred_over_the_runner_stderr(self):
        from app.main import stopped_entry
        # M10 (2026-10-19): the runner stderr only said "Chaos v1 experiment failed."
        result = {'chaosMode': 'litmus_pod_network_latency', 'infraError': 'litmus_fault_execution_failed',
                  'stderr': 'Chaos v1 experiment failed.',
                  'chaosResult': {'raw': {'experimentStatus': {'errorOutput': {
                      'errorCode': 'HELPER_ERROR',
                      'reason': '{"source":"pod-network-latency-helper-qxmhge","errorCode":"HELPER_ERROR",'
                                '"phase":"ChaosInject","reason":"failed to create tc rules: '
                                'Error: Specified qdisc kind is unknown.\\n :"}'}}}}}
        self.assertEqual(stopped_entry(result)['reason'],
                         'HELPER_ERROR ChaosInject: failed to create tc rules: Error: Specified qdisc kind is unknown.')
