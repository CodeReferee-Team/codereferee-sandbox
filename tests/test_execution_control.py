import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from scripts.execution_control import begin_cleanup, record_resource, remaining_timeout
from scripts.request_recovery import create_inventory, recover_inventory, cleanup_workspace
from scripts import install_observability as wiring
from repository_checks import CheckCommandTimeout, run_check_command


class ExecutionControlTests(unittest.TestCase):
    def test_inventory_is_written_before_resource_creation_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = create_inventory(Path(directory), 'exec-' + 'a'*24)
            with patch.dict(os.environ, {'CODEREFEREE_EXECUTION_JOURNAL': str(path)}):
                record_resource('images', 'example')
                record_resource('images', 'example')
            self.assertEqual(json.loads(path.read_text())['images'], ['example'])

    def test_expired_work_budget_allows_separate_cleanup(self):
        with patch.dict(os.environ, {'CODEREFEREE_EXECUTION_DEADLINE': '1'}):
            with self.assertRaises(subprocess.TimeoutExpired):
                remaining_timeout(30)
            begin_cleanup()
            self.assertEqual(remaining_timeout(30), 30)

    def test_namespace_absence_is_required_before_kind_image_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            resource = 'exec-' + 'a'*24
            path = create_inventory(Path(directory), resource)
            image = 'codereferee/' + resource + ':' + 'b'*12
            data = json.loads(path.read_text())
            data.update(images=[image], loaded_images=[image], namespaces=['codereferee-' + resource])
            path.write_text(json.dumps(data))
            with patch('scripts.request_recovery.delete_namespace_confirmed', return_value=False), \
                    patch('scripts.request_recovery.cleanup_request_image', return_value={'errors': []}) as clean:
                answer = recover_inventory(path)
            self.assertFalse(clean.call_args.kwargs['remove_from_kind'])
            self.assertFalse(answer['complete'])

    def test_workspace_cleanup_rejects_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = root / 'other-project'
            unrelated.mkdir()
            with self.assertRaises(ValueError):
                cleanup_workspace(unrelated, root)
            self.assertTrue(unrelated.is_dir())

    def test_transient_download_retries_share_the_phase_budget(self):
        results = [subprocess.CompletedProcess(['pip'], 1, '', 'ECONNRESET'),
                   subprocess.CompletedProcess(['pip'], 1, '', 'RemoteDisconnected'),
                   subprocess.CompletedProcess(['pip'], 0, 'installed', '')]
        with patch.dict(os.environ, {'CODEREFEREE_RETRY_DOWNLOADS': '1'}), \
                patch('repository_checks.subprocess.run', side_effect=results) as run, \
                patch('repository_checks.time.sleep') as pause:
            answer, attempts = run_check_command(['pip'], {}, 'install', time.monotonic()+30)
        self.assertEqual(answer.returncode, 0)
        self.assertEqual(len(attempts), 3)
        self.assertEqual([call.args[0] for call in pause.call_args_list], [2, 4])
        self.assertTrue(all(call.kwargs['timeout'] <= 30 for call in run.call_args_list))

    def test_package_configuration_failure_stops_the_install(self):
        failed = subprocess.CompletedProcess(['pip'], 1, '', 'No matching distribution found')
        with patch.dict(os.environ, {'CODEREFEREE_RETRY_DOWNLOADS': '1'}), \
                patch('repository_checks.subprocess.run', return_value=failed) as run:
            answer, attempts = run_check_command(['pip'], {}, 'install', time.monotonic()+10)
        self.assertEqual(answer.returncode, 1)
        self.assertEqual(run.call_count, 1)

    def test_download_backoff_is_included_in_the_phase_deadline(self):
        sleep = time.sleep
        failed = subprocess.CompletedProcess(['pip'], 1, '', 'ECONNRESET')
        with patch.dict(os.environ, {'CODEREFEREE_RETRY_DOWNLOADS': '1'}), \
                patch('repository_checks.subprocess.run', return_value=failed), \
                patch('repository_checks.time.sleep', side_effect=lambda seconds: sleep(.08)):
            with self.assertRaises(CheckCommandTimeout) as caught:
                run_check_command(['pip'], {}, 'install', time.monotonic()+.05)
        self.assertEqual(len(caught.exception.attempts), 1)

    def test_certificate_configuration_error_is_not_retried(self):
        failed = subprocess.CompletedProcess(['pip'], 1, '', 'CERTIFICATE_VERIFY_FAILED')
        with patch.dict(os.environ, {'CODEREFEREE_RETRY_DOWNLOADS': '1'}), \
                patch('repository_checks.subprocess.run', return_value=failed) as run:
            answer, attempts = run_check_command(['pip'], {}, 'install', time.monotonic()+10)
        self.assertEqual(answer.returncode, 1)
        self.assertEqual(run.call_count, 1)

    def test_observation_commands_use_remaining_request_budget(self):
        with wiring.execution_budget(time.monotonic()+2), \
                patch.object(wiring.subprocess, 'run', return_value=subprocess.CompletedProcess(['kubectl'], 0, '', '')) as run:
            wiring.run(['kubectl', 'get', 'pods'])
        self.assertLessEqual(run.call_args.kwargs['timeout'], 2)
        with wiring.execution_budget(time.monotonic()-1):
            with self.assertRaises(TimeoutError):
                wiring.run(['kubectl', 'get', 'pods'])


if __name__ == '__main__':
    unittest.main()
