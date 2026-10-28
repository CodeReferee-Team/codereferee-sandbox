import json
from pathlib import Path
import sys
import unittest
import io
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from target_oom import helper_manifest, oom_evidence, run_target_oom, HELPER_CODE

CID = 'containerd://' + 'a' * 64
START = '2026-10-28T00:00:00Z'

def pod():
    return {'metadata': {'name': 'app-1', 'uid': 'pod-uid'}, 'spec': {'nodeName': 'node-1'},
            'status': {'containerStatuses': [{'name': 'app', 'ready': True, 'restartCount': 0, 'containerID': CID}]}}

def killed():
    value = pod()
    status = value['status']['containerStatuses'][0]
    status.update(containerID='containerd://'+'b'*64, restartCount=1,
                  lastState={'terminated': {'reason': 'OOMKilled', 'exitCode': 137,
                      'containerID': CID, 'finishedAt': '2026-10-28T00:00:01Z'}})
    return value

class OomProofTests(unittest.TestCase):
    def test_exact_app_container_oom_is_required(self):
        self.assertIsNotNone(oom_evidence(pod(), killed(), 'app', START))
        for field, value in [('reason', 'Error'), ('exitCode', 0), ('containerID', 'other'),
                             ('finishedAt', '2026-10-27T23:59:59Z')]:
            after = killed()
            after['status']['containerStatuses'][0]['lastState']['terminated'][field] = value
            self.assertIsNone(oom_evidence(pod(), after, 'app', START))
        after = killed(); after['metadata']['uid'] = 'replacement'
        self.assertIsNone(oom_evidence(pod(), after, 'app', START))
        self.assertIsNone(oom_evidence(pod(), killed(), 'sidecar', START))
        after = killed()
        after['status']['containerStatuses'][0]['lastState']['terminated']['finishedAt'] = START
        self.assertIsNotNone(oom_evidence(pod(), after, 'app', '2026-10-28T00:00:00.900Z'))

    def test_helper_is_outside_target_with_bounded_lifetime(self):
        manifest = helper_manifest('request', 'helper', pod(), 'app')
        self.assertEqual(manifest['spec']['nodeName'], 'node-1')
        self.assertEqual(manifest['spec']['activeDeadlineSeconds'], 45)
        self.assertEqual(manifest['spec']['containers'][0]['imagePullPolicy'], 'IfNotPresent')
        self.assertFalse(manifest['spec']['automountServiceAccountToken'])
        self.assertEqual(manifest['spec']['volumes'][0]['hostPath']['path'], '/sys/fs/cgroup')
        for cid in ('docker://'+'a'*64, 'containerd://../../', ''):
            p = pod(); p['status']['containerStatuses'][0]['containerID'] = cid
            with self.assertRaises(ValueError): helper_manifest('request', 'helper', p, 'app')
        compile(HELPER_CODE, '<oom-helper>', 'exec')

    def test_outcome_needs_both_target_oom_and_recovery(self):
        class Observer:
            def collect(self, _): return [{'success': True}]
        def run(command, **kwargs):
            import subprocess
            args = command[3:]
            if 'logs' in command: value = {'limit_written': True, 'restore': 'restored'}
            elif 'get' in command:
                value = killed() if 'app-1' in command else {'status': {'phase': 'Succeeded'}}
            else: value = {}
            return subprocess.CompletedProcess(command, 0, json.dumps(value), '')
        with patch('target_oom.subprocess.run', side_effect=run):
            result, probes = run_target_oom('request', [pod()], 'app', Observer(), 5, START)
        self.assertEqual(result['exitCode'], 0)
        self.assertTrue(result['oom_observed'])
        self.assertGreaterEqual(len(probes), 5)

    def test_unsupported_runtime_returns_infrastructure_not_success(self):
        p = pod(); p['status']['containerStatuses'][0]['containerID'] = 'docker://unknown'
        with patch('target_oom.subprocess.run') as run:
            result, _ = run_target_oom('request', [p], 'app', None, 1, START)
        self.assertFalse(result['oom_observed'])
        self.assertIsNone(result['exitCode'])
        self.assertIsNone(result['recovered'])
        self.assertEqual(result['infraError'], 'oom_injection_failed')
        # A locally rejected manifest must not delete an unrelated Pod.
        run.assert_not_called()

    def test_no_oom_is_not_a_recovery_success(self):
        import subprocess
        with patch('target_oom.time.monotonic', side_effect=[0, 2]), \
             patch('target_oom.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '{}', '')):
            result, _ = run_target_oom('request', [pod()], 'app', None, 1, START)
        self.assertEqual(result['infraError'], 'oom_not_triggered')
        self.assertIsNone(result['exitCode'])
        self.assertIsNone(result['recovered'])

    def test_genuine_oom_but_no_http_recovery_is_resilience_failure(self):
        import subprocess
        class Observer:
            def collect(self, _): return [{'success': False}]
        def run(command, **kwargs):
            if 'logs' in command: value = {'limit_written': True, 'restore': 'target_cgroup_removed'}
            elif 'get' in command: value = killed() if 'app-1' in command else {'status': {'phase': 'Succeeded'}}
            else: value = {}
            return subprocess.CompletedProcess(command, 0, json.dumps(value), '')
        with patch('target_oom.time.monotonic', side_effect=[0, 0, 2]), \
             patch('target_oom.subprocess.run', side_effect=run):
            result, _ = run_target_oom('request', [pod()], 'app', Observer(), 1, START)
        self.assertTrue(result['oom_observed'])
        self.assertEqual(result['observationStatus'], 'observed')
        self.assertEqual(result['exitCode'], 1)
        self.assertTrue(result['timedOut'])

    def test_helper_failure_or_failed_restoration_cannot_become_recovery_success(self):
        import subprocess
        class Observer:
            def collect(self, _): return [{'success': True}]
        for report, expected in [({'limit_written': True, 'restore': 'restored',
                                  'error': 'target_oom_not_observed'}, 'oom_not_triggered'),
                                 ({'limit_written': True, 'restore': 'failed'}, 'oom_restore_failed')]:
            def run(command, **kwargs):
                if 'logs' in command: value = report
                elif 'get' in command: value = killed() if 'app-1' in command else {'status': {'phase': 'Succeeded'}}
                else: value = {}
                return subprocess.CompletedProcess(command, 0, json.dumps(value), '')
            with patch('target_oom.subprocess.run', side_effect=run):
                result, _ = run_target_oom('request', [pod()], 'app', Observer(), 1, START)
            self.assertEqual(result['infraError'], expected)
            self.assertEqual(result['observationStatus'], 'infrastructure_error')
            self.assertIsNone(result['exitCode'])
            self.assertIsNone(result['recovered'])

    def test_helper_restores_exact_limit_for_both_systemd_prefixes(self):
        uid = '12345678-1111-2222-3333-123456789abc'
        for prefix in ('kubepods-burstable', 'kubelet-kubepods-burstable'):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/'cgroup.controllers').touch()
                group = root/(prefix+'-pod'+uid.replace('-', '_')+'.slice')/('cri-containerd-'+'a'*64+'.scope')
                group.mkdir(parents=True)
                (group/'memory.max').write_text(str(64*1024**2))
                (group/'memory.events.local').write_text('oom_kill 0\n')
                code = HELPER_CODE.replace("Path('/host-cgroup')", f'Path({str(root)!r})')
                output = io.StringIO()
                with patch.object(sys, 'argv', ['helper', 'a'*64, uid]), redirect_stdout(output), \
                     patch('time.monotonic', side_effect=[0, 16]), patch('signal.signal'):
                    exec(code, {})
                self.assertEqual(json.loads(output.getvalue())['restore'], 'restored')
                self.assertEqual((group/'memory.max').read_text(), str(64*1024**2))

if __name__ == '__main__': unittest.main()
