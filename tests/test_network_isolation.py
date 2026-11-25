"""외부 차단 상태를 지어내지 않고 실제 규칙에서 읽는지."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import network_isolation
from network_isolation import external_egress_state

BLOCKING = ('-N CODEREFEREE-EGRESS\n'
            '-A CODEREFEREE-EGRESS -d 10.0.0.0/8 -j RETURN\n'
            '-A CODEREFEREE-EGRESS -p udp -m udp --dport 53 -j RETURN\n'
            '-A CODEREFEREE-EGRESS -j REJECT --reject-with icmp-port-unreachable\n')
EMPTY_CHAIN = '-N CODEREFEREE-EGRESS\n'


def responses(nodes='codereferee-control-plane\n', *, attached=0, chain=BLOCKING):
    def fake(command, **kwargs):
        if command[0] != 'docker':
            return subprocess.CompletedProcess(command, 0, nodes, '')
        if '-C' in command:
            return subprocess.CompletedProcess(command, attached, '', '')
        return subprocess.CompletedProcess(command, 0, chain, '')
    return fake


class ExternalEgressStateTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'CODEREFEREE_CLUSTER_PROVIDER': 'kind'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(patch.stopall)
        patch('network_isolation.kubectl_environment', return_value={}).start()

    def test_a_chain_that_rejects_is_reported_as_blocked_with_its_rules(self):
        with patch('network_isolation.subprocess.run', side_effect=responses()):
            state = external_egress_state()
        self.assertEqual(state['external_egress'], 'blocked')
        self.assertEqual(state['scope'], '10.244.0.0/16')
        self.assertEqual(state['still_allowed'], ['cluster_internal', 'dns'])
        # 근거를 같이 싣는다. 상태만 적으면 나중에 확인할 방법이 없다.
        self.assertIn('-A CODEREFEREE-EGRESS -j REJECT --reject-with icmp-port-unreachable',
                      state['rules']['codereferee-control-plane'])
        self.assertIn('did not work', state['measurement_note'])

    def test_a_missing_jump_is_reported_as_allowed(self):
        """노드를 다시 만들면 규칙이 사라진다. 그때 blocked 라고 하면 리포트가 거짓말한다."""
        with patch('network_isolation.subprocess.run', side_effect=responses(attached=1)):
            state = external_egress_state()
        self.assertEqual(state['external_egress'], 'allowed')
        self.assertEqual(state['nodes'], ['codereferee-control-plane'])

    def test_an_attached_but_empty_chain_is_not_blocking(self):
        """체인을 걸어 두기만 하고 비우면 전부 통과한다. 걸렸다는 사실만으로 막혔다고 하면 안 된다."""
        with patch('network_isolation.subprocess.run', side_effect=responses(chain=EMPTY_CHAIN)):
            self.assertEqual(external_egress_state()['external_egress'], 'allowed')

    def test_one_open_node_among_several_makes_the_whole_run_open(self):
        def fake(command, **kwargs):
            if command[0] != 'docker':
                return subprocess.CompletedProcess(command, 0, 'node-a\nnode-b\n', '')
            code = 0 if command[2] == 'node-a' else 1
            if '-C' in command:
                return subprocess.CompletedProcess(command, code, '', '')
            return subprocess.CompletedProcess(command, 0, BLOCKING, '')
        with patch('network_isolation.subprocess.run', side_effect=fake):
            state = external_egress_state()
        self.assertEqual((state['external_egress'], state['nodes']), ('allowed', ['node-b']))

    def test_a_failure_to_read_says_unknown_rather_than_guessing(self):
        with patch('network_isolation.subprocess.run', side_effect=OSError('docker missing')):
            self.assertEqual(external_egress_state()['external_egress'], 'unknown')

    def test_a_non_kind_cluster_says_unknown(self):
        with patch.dict('os.environ', {'CODEREFEREE_CLUSTER_PROVIDER': 'eks'}):
            self.assertEqual(external_egress_state(),
                             {'external_egress': 'unknown', 'reason': 'cluster_provider_not_kind'})
