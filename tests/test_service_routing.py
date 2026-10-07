import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_service_selector_experiment as routing


class ServiceRoutingTests(unittest.TestCase):
    def test_original_selector_is_restored_when_fault_observation_fails(self):
        args = SimpleNamespace(namespace='test', deployment='api', service='api', service_port=80,
            label_selector='app=api', local_port=18082, request_timeout_seconds=2,
            baseline_probes=1, recovery_timeout_seconds=60)
        observer = MagicMock()
        observer.__enter__.return_value.collect.return_value = [{'success': True}]
        original = {'app': 'api'}
        with patch.object(routing, 'parse_args', return_value=args), \
             patch.object(routing, 'get_ready_pod', return_value={'uid': 'test'}), \
             patch.object(routing, 'get_target_configuration', return_value={'replicas': 1}), \
             patch.object(routing, 'service_selector', return_value=original), \
             patch.object(routing, 'InClusterProbe', return_value=observer), \
             patch.object(routing, 'replace_selector') as replace, \
             patch.object(routing, 'wait_for_endpoint_state', side_effect=TimeoutError('test')):
            with self.assertRaises(TimeoutError):
                routing.main()
        self.assertEqual(replace.call_args_list[-1], call('test', 'api', original))
