import subprocess
import os
import tempfile
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from app.main import (RepositoryValidationRequest, StageTimeout, run_bounded,
                      validate_repository)


class BoundedRunTests(unittest.TestCase):
    def test_timeout_kills_grandchildren(self):
        # The child starts a grandchild that would outlive subprocess.run(timeout=).
        script = ('import subprocess, sys, time\n'
                  'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
                  'print(p.pid, flush=True)\n'
                  'time.sleep(30)\n')
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'pid'
            script = script.replace('print(p.pid, flush=True)',
                f'from pathlib import Path; Path({str(pid_file)!r}).write_text(str(p.pid))')
            with self.assertRaises(StageTimeout) as caught:
                run_bounded([sys.executable, '-c', script], stage='deploy', seconds=1)
            pid = int(pid_file.read_text())
        # Killing only the child leaves the grandchild holding stdout for 30 s.
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(caught.exception.stage, 'deploy')
        if os.name == 'nt':
            listing = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                                     capture_output=True).stdout.decode(errors='replace')
            self.assertNotIn(f'"{pid}"', listing)
            return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            listing = subprocess.run(['ps', '-eo', 'stat,args'], capture_output=True, text=True).stdout
            alive = [line for line in listing.splitlines()
                     if 'time.sleep(30)' in line and not line.startswith('Z') and 'ps -eo' not in line]
            if not alive:
                break
            time.sleep(0.1)
        self.assertEqual(alive, [])


class StoppedStageTests(unittest.TestCase):
    @patch('app.main.cleanup_namespace')
    @patch('app.main.deploy_repository')
    def test_deploy_timeout_says_where_it_stopped(self, deploy, cleanup):
        deploy.side_effect = StageTimeout('deploy', 1200)
        request = RepositoryValidationRequest(repositoryUrl='https://github.com/test/repo', requestId='t',
                                              deploymentProfile='p', chaosMode='litmus_pod_delete')
        result = validate_repository(request)
        self.assertEqual(result['observationStatus'], 'infrastructure_error')
        self.assertEqual(result['infraError'], 'sandbox_execution_timeout')
        self.assertEqual(result['stoppedAt'], 'deploy')
        self.assertTrue(result['timedOut'])

    @patch('app.main.cleanup_namespace')
    @patch('app.main.deploy_repository')
    def test_deploy_failure_says_where_it_stopped(self, deploy, cleanup):
        deploy.side_effect = RuntimeError('kind load failed')
        request = RepositoryValidationRequest(repositoryUrl='https://github.com/test/repo', requestId='t',
                                              deploymentProfile='p', chaosMode='litmus_pod_delete')
        result = validate_repository(request)
        self.assertEqual(result['stoppedAt'], 'deploy')
        self.assertEqual(result['stderr'], 'kind load failed')


if __name__ == '__main__':
    unittest.main()
