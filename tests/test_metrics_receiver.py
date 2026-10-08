import unittest
from scripts.bootstrap_metrics_receiver import validate_receiver


class MetricsReceiverTests(unittest.TestCase):
    def valid(self):
        return {'Config': {'Cmd': ['--web.enable-remote-write-receiver']},
            'NetworkSettings': {'Networks': {'kind': {}}},
            'HostConfig': {'PortBindings': {'9090/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '19091'}]}},
            'Mounts': [{'Type': 'volume', 'Destination': '/prometheus'}]}

    def test_valid_receiver_is_reusable(self):
        validate_receiver(self.valid())

    def test_incompatible_receiver_is_not_silently_replaced(self):
        for key in ('Config', 'NetworkSettings', 'HostConfig', 'Mounts'):
            container = self.valid()
            container.pop(key)
            with self.assertRaises(RuntimeError):
                validate_receiver(container)
