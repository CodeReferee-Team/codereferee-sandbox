import contextlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_deployment_scale_experiment as scale_runner
import run_rollout_restart_experiment as rollout_runner
from in_cluster_probe import InClusterProbe


def arguments():
    return SimpleNamespace(namespace='codereferee-test', deployment='api', service='api',
        service_port=80, label_selector='app=api', baseline_probes=2, recovery_probes=5,
        request_timeout_seconds=2, recovery_timeout_seconds=1, local_port=18080,
        fault_seconds=0, output=None)


def healthy_probes():
    return [{'at': f'2026-10-07T00:00:0{i}Z', 'success': True,
             'status_code': 200, 'latency_ms': 1} for i in range(5)]


class LifecycleObservationTests(unittest.TestCase):
    def test_scale_recovery_timeout_is_observed_failure_and_restores_replicas(self):
        observer = MagicMock()
        observer.__enter__.return_value = observer
        observer.collect.return_value = healthy_probes()[:2]
        failed = dict(healthy_probes()[0], success=False, status_code=None)
        observer.collect_until_healthy.return_value = ([failed], False)
        output = io.StringIO()
        with patch.object(scale_runner, 'parse_args', return_value=arguments()), \
             patch.object(scale_runner, 'get_ready_pod', return_value={'uid': 'old'}), \
             patch.object(scale_runner, 'get_target_configuration', return_value={'replicas': 2}), \
             patch.object(scale_runner, 'InClusterProbe', return_value=observer), \
             patch.object(scale_runner, 'scale') as scale, contextlib.redirect_stdout(output):
            self.assertEqual(scale_runner.main(), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result['observationStatus'], 'observed')
        self.assertTrue(result['timedOut'])
        self.assertFalse(result['chaos_observation']['recovered'])
        self.assertEqual([c.args[-1] for c in scale.call_args_list], [0, 2, 2])

    def test_rollout_requires_replacement_uid_and_healthy_http(self):
        args = arguments()
        args.recovery_probes = 1
        observer = MagicMock()
        observer.__enter__.return_value = observer
        observer.collect.return_value = healthy_probes()[:2]
        observer.probe.return_value = healthy_probes()[0]
        output = io.StringIO()
        with patch.object(rollout_runner, 'parse_args', return_value=args), \
             patch.object(rollout_runner, 'get_ready_pod', return_value={'uid': 'old'}), \
             patch.object(rollout_runner, 'get_target_configuration', return_value={'replicas': 1}), \
             patch.object(rollout_runner, 'InClusterProbe', return_value=observer), \
             patch.object(rollout_runner, 'ready_pods', side_effect=[[{'uid': 'old'}], [{'uid': 'new'}]]), \
             patch.object(rollout_runner, 'kubectl'), contextlib.redirect_stdout(output):
            self.assertEqual(rollout_runner.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['probeTransport'], 'in_cluster_http')
        self.assertEqual(result['chaos_observation']['replacement_pod_uid'], 'new')
        self.assertTrue(result['source']['real_execution_observed'])

    def test_healthy_recovery_requires_consecutive_successes(self):
        observer = InClusterProbe('test', 'api', 80)
        results = [{'success': x} for x in [True, False, True, True]]
        with patch.object(observer, 'probe', side_effect=results), patch('in_cluster_probe.time.sleep'):
            probes, recovered = observer.collect_until_healthy(10, required=2)
        self.assertTrue(recovered)
        self.assertEqual(len(probes), 4)
