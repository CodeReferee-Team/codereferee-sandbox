import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_deployment_scale_experiment as scale

TARGET = {'namespace': 'test', 'deployment': 'api', 'service': 'api', 'servicePort': 8000,
          'labelSelector': 'app=api'}


class ScaleConfirmationTests(unittest.TestCase):
    def test_waits_for_actual_pod_deletion_not_just_scale_command_acceptance(self):
        replies = [subprocess.CompletedProcess([], 0, json.dumps({'items': [{'metadata': {'deletionTimestamp': 'now'}}]}), ''),
                   subprocess.CompletedProcess([], 0, '{"items": []}', '')]
        with patch.object(scale.subprocess, 'run', side_effect=replies) as run, patch.object(scale.time, 'sleep'):
            scale.wait_for_zero_pods(TARGET, 5)
        self.assertEqual(run.call_count, 2)

    def test_unconfirmed_scale_is_not_returned_as_observed_success(self):
        with patch.object(scale.time, 'monotonic', side_effect=[0, 0, 2]), \
             patch.object(scale.time, 'sleep'), patch.object(scale.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0, '{"items": [{}]}', '')):
            with self.assertRaisesRegex(RuntimeError, 'Scale-down was not observed'):
                scale.wait_for_zero_pods(TARGET, 1)

    def test_original_replica_count_is_restored_even_if_confirmation_fails(self):
        observer = MagicMock()
        observer.__enter__.return_value = observer
        observer.collect.return_value = [{'success': True}]
        args = SimpleNamespace(local_port=18081, request_timeout_seconds=2, baseline_probes=1,
                               recovery_timeout_seconds=5)
        with patch.object(scale, 'parse_args', return_value=args), patch.object(scale, 'load_target', return_value=TARGET), \
             patch.object(scale, 'get_ready_pod', return_value={'uid': 'uid'}), \
             patch.object(scale, 'get_target_configuration', return_value={'replicas': 2}), \
             patch.object(scale, 'InClusterProbe', return_value=observer), patch.object(scale, 'scale') as change, \
             patch.object(scale, 'wait_for_zero_pods', side_effect=RuntimeError('confirmation failed')):
            with self.assertRaisesRegex(RuntimeError, 'confirmation failed'): scale.main()
        self.assertEqual([c.args[-1] for c in change.call_args_list], [0, 2])


if __name__ == '__main__':
    unittest.main()
