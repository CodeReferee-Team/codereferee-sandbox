import sys
import io
import json
import os
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from run_litmus_pod_delete import fault_environment, classify_result, measured_recovery_seconds, memory_megabytes, oom_during_experiment
from in_cluster_probe import OBSERVER_CODE


class RuntimeFaultTests(unittest.TestCase):
    def test_workflow_timeout_after_success_is_not_counted_as_success(self):
        environment = {'TARGET_URL': 'http://test', 'PROBE_TIMEOUT': '2',
                       'PROBE_SPEC': json.dumps({'steps': [{'path': '/orders'}, {'path': '/payment'}]})}
        scope = {}
        with patch.dict(os.environ, environment):
            exec(OBSERVER_CODE.rsplit('HTTPServer((', 1)[0], scope)
        attempts = iter([200, TimeoutError('redis timeout')])
        def call_step(step):
            result = next(attempts)
            if isinstance(result, Exception):
                raise result
            return result
        scope['call'] = call_step
        handler = object.__new__(scope['Handler'])
        handler.wfile = io.BytesIO()
        handler.send_response = lambda *args: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.do_GET()
        result = json.loads(handler.wfile.getvalue())
        self.assertFalse(result['success'])
        self.assertIsNone(result['status_code'])
        self.assertEqual(result['error'], 'redis timeout')
    def test_observer_http_has_framed_response_and_valid_code(self):
        compile(OBSERVER_CODE, '<observer>', 'exec')
        self.assertIn('Content-Length', OBSERVER_CODE)

    def test_memory_faults_are_sized_against_declared_limit(self):
        self.assertEqual(memory_megabytes('1Gi'), 1024)
        self.assertEqual(memory_megabytes('128Mi'), 128)
        self.assertEqual(fault_environment('pod_memory_oom', 'api', 15, 160)['MEMORY_CONSUMPTION'], '160')
        with self.assertRaises(ValueError):
            memory_megabytes('')
    def test_recovery_excludes_runner_setup_and_preserves_unrecovered(self):
        probes = [{'at': f'2026-10-07T00:00:{i:02d}Z', 'success': i != 1} for i in range(8)]
        self.assertEqual(measured_recovery_seconds(probes), 1)
        self.assertIsNone(measured_recovery_seconds(probes[:2]))
        self.assertEqual(measured_recovery_seconds([probes[0]]), 0)
    def test_runtime_faults_use_explicit_cri_and_target_container(self):
        env = fault_environment('container_kill', 'worker', 15)
        self.assertEqual(env['TARGET_CONTAINER'], 'worker')
        self.assertEqual(env['CONTAINER_RUNTIME'], 'containerd')
        self.assertEqual(env['SOCKET_PATH'], '/run/containerd/containerd.sock')
        self.assertEqual(env['CHAOS_INTERVAL'], '60')

    def test_network_parameters_are_bounded(self):
        env = fault_environment('pod_network_latency', 'api', 15)
        self.assertEqual(env['NETWORK_LATENCY'], '300')
        self.assertEqual(env['NETWORK_INTERFACE'], 'eth0')
        with self.assertRaises(ValueError):
            fault_environment('pod_cpu_hog', 'api', 0)

    def test_runtime_failure_is_not_user_code_failure(self):
        result = {'verdict': 'Fail', 'raw': {'experimentStatus': {
            'errorOutput': {'errorCode': 'CONTAINER_RUNTIME_ERROR', 'reason': 'CRI unavailable'}}}}
        self.assertEqual(classify_result(result), 'infrastructure_error')

    def test_injected_fault_with_application_health_failure_is_observed(self):
        result = {'verdict': 'Fail', 'raw': {'experimentStatus': {
            'errorOutput': {'errorCode': 'STATUS_CHECKS_ERROR', 'reason': 'post-chaos health check failed'}},
            'history': {'targets': [{'chaosStatus': 'reverted'}]}}}
        self.assertEqual(classify_result(result), 'observed')

    def test_pre_injection_health_failure_and_old_oom_are_not_mislabelled(self):
        result = {'verdict': 'Fail', 'raw': {'experimentStatus': {
            'errorOutput': {'errorCode': 'STATUS_CHECKS_ERROR'}}}}
        self.assertEqual(classify_result(result), 'infrastructure_error')
        termination = {'reason': 'OOMKilled', 'finishedAt': '2026-10-07T00:00:00Z'}
        self.assertFalse(oom_during_experiment([termination], '2026-10-07T00:01:00Z', 2, 3))
        self.assertTrue(oom_during_experiment([termination], '2026-10-06T23:59:59Z', 2, 3))
