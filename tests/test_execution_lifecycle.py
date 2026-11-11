import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.executions import ExecutionSlot, ExecutionStore, execution_context
from app.main import RepositoryValidationRequest, app
from app.validation_lifecycle import recover_previous, submit
from scripts.execution_control import remaining_timeout
from scripts.request_recovery import create_inventory, recover_inventory


def result(code=0, *, timed_out=False, infra=False, message=''):
    return {'observationStatus': 'infrastructure_error' if infra else 'observed',
            'exitCode': None if infra else code, 'timedOut': timed_out, 'stderr': message, 'source': {}}


def progress(phase, commit=None):
    path = execution_context.get()['journal']
    data = json.loads(path.read_text())
    data['phase'] = phase
    if commit:
        data['commits'] = [commit]
    path.write_text(json.dumps(data))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'CODEREFEREE_EXECUTION_STATE_DIR': str(self.directory)})
        self.environment.start()
        self.request = RepositoryValidationRequest(repositoryUrl='https://github.com/example/app',
            requestId='same-logical-job', deadlineSeconds=300, preparationTimeoutSeconds=5)

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_same_request_replays_completed_result_without_executing(self):
        calls = []
        def handler(request):
            calls.append(request.request_id)
            return result()
        first = submit(self.request, handler)
        repeated = submit(self.request, handler)
        self.assertEqual(calls, ['same-logical-job'])
        self.assertTrue(repeated['source']['execution']['replayed'])
        self.assertEqual(first['source']['execution']['executionId'], repeated['source']['execution']['executionId'])

    def test_patch_change_has_a_new_execution_and_resource_identity(self):
        first = submit(self.request, lambda request: result())
        changed = self.request.model_copy(update={'patch_diff': 'different patch'})
        second = submit(changed, lambda request: result())
        self.assertNotEqual(first['source']['execution']['executionId'], second['source']['execution']['executionId'])
        self.assertNotEqual(first['source']['execution']['attempts'][0]['resourceId'],
                            second['source']['execution']['attempts'][0]['resourceId'])

    def test_active_duplicate_is_busy_and_slot_is_released_after_completion(self):
        entered, release = threading.Event(), threading.Event()
        answers = []
        def handler(request):
            entered.set()
            release.wait(5)
            return result()
        thread = threading.Thread(target=lambda: answers.append(submit(self.request, handler)))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(HTTPException) as busy:
                submit(self.request, lambda request: self.fail('Duplicate execution started'))
            self.assertEqual(busy.exception.status_code, 409)
            self.assertIn('Retry-After', busy.exception.headers)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(len(answers), 1)
        different = self.request.model_copy(update={'request_id': 'next-job'})
        self.assertEqual(submit(different, lambda request: result())['exitCode'], 0)

    def test_timeout_retry_doubles_only_failed_phase_and_pins_commit(self):
        calls = []
        def handler(request):
            calls.append(request)
            progress('install', 'a'*40)
            return result(timed_out=True, infra=True) if len(calls) == 1 else result()
        request = self.request.model_copy(update={'preparation_retries': 1, 'phase_timeouts': {'install': 4}})
        answer = submit(request, handler)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call.request_id == 'same-logical-job' for call in calls))
        self.assertEqual(calls[1].phase_timeouts['install'], 8)
        self.assertEqual(calls[1].preparation_timeout_seconds, 10)
        self.assertEqual(calls[1].commit_sha, 'a'*40)
        history = answer['source']['execution']['attempts']
        self.assertEqual(history[0]['retryReason'], 'preparation_timeout')
        self.assertNotEqual(history[0]['resourceId'], history[1]['resourceId'])
        self.assertTrue(all(attempt['cleanupComplete'] for attempt in history))

    def test_transient_retry_uses_same_time_limit(self):
        calls = []
        def handler(request):
            calls.append(request.preparation_timeout_seconds)
            progress('install')
            return result(infra=True, message='RemoteDisconnected') if len(calls) == 1 else result()
        submit(self.request.model_copy(update={'preparation_retries': 1}), handler)
        self.assertEqual(calls, [5, 5])

    def test_repository_snapshots_survive_preparation_retry(self):
        calls = []
        snapshots = {'backend': {'url': 'https://github.com/example/backend', 'commitSha': 'b'*40}}
        def handler(request):
            context = execution_context.get()
            calls.append(dict(context['repository_snapshots']))
            progress('clone')
            path = context['journal']
            data = json.loads(path.read_text())
            data['repository_snapshots'] = snapshots
            path.write_text(json.dumps(data))
            return result(timed_out=True, infra=True) if len(calls)==1 else result()
        submit(self.request.model_copy(update={'preparation_retries': 1}), handler)
        self.assertEqual(calls, [{}, snapshots])

    def test_observed_failure_is_preserved(self):
        calls = []
        def handler(request):
            calls.append(True)
            progress('test')
            return result(code=1, message='RemoteDisconnected in an application assertion')
        answer = submit(self.request.model_copy(update={'preparation_retries': 1}), handler)
        self.assertEqual(calls, [True])
        self.assertEqual(answer['exitCode'], 1)

    def test_retry_requires_remaining_total_budget(self):
        calls = []
        def handler(request):
            calls.append(True)
            progress('install')
            return result(timed_out=True, infra=True)
        submit(self.request.model_copy(update={'preparation_retries': 1, 'deadline_seconds': 90}), handler)
        self.assertEqual(calls, [True])

    def test_interrupted_execution_is_recovered_before_next_execution(self):
        store = ExecutionStore()
        identity = store.identity(self.request)
        store.start(identity, self.request.request_id)
        resource = 'exec-' + 'a'*24
        create_inventory(store.directory, resource)
        store.attempt(identity, resource)
        recover_previous(store)
        recovered = json.loads(store.get(identity)['result'])
        self.assertEqual(recovered['infraError'], 'sandbox_worker_interrupted')
        self.assertTrue(recovered['source']['execution_recovery']['complete'])

    def test_pending_cleanup_blocks_new_work(self):
        store = ExecutionStore()
        store.start('older', 'old-request')
        resource = 'exec-' + 'a'*24
        create_inventory(store.directory, resource)
        store.attempt('older', resource)
        with patch('app.validation_lifecycle.recover_inventory', return_value={'complete': False}):
            with self.assertRaises(HTTPException) as blocked:
                submit(self.request, lambda request: self.fail('Cleanup was incomplete'))
        self.assertEqual(blocked.exception.status_code, 503)

    def test_missing_recovery_inventory_blocks_new_work(self):
        store = ExecutionStore()
        store.start('missing', 'old-request')
        store.attempt('missing', 'exec-' + 'a'*24)
        with self.assertRaises(HTTPException) as blocked:
            submit(self.request, lambda request: self.fail('Recovery was unverified'))
        self.assertEqual(blocked.exception.status_code, 503)
        self.assertEqual(store.get('missing')['state'], 'cleanup_pending')

    def test_failed_attempt_cleanup_prevents_retry(self):
        def handler(request):
            progress('install')
            return result(timed_out=True, infra=True)
        with patch('app.validation_lifecycle.recover_inventory', return_value={'complete': False}) as cleanup:
            answer = submit(self.request.model_copy(update={'preparation_retries': 1}), handler)
        self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(len(answer['source']['execution']['attempts']), 1)
        self.assertEqual(answer['source']['execution']['state'], 'cleanup_pending')

    def test_http_entry_point_replay_and_status(self):
        with patch('app.main.validate_repository', return_value=result()) as handler, TestClient(app) as client:
            payload = self.request.model_dump(by_alias=True)
            self.assertEqual(client.post('/repositories/validate', json=payload).status_code, 200)
            repeated = client.post('/repositories/validate', json=payload).json()
            status = client.get('/executions', params={'requestId': 'same-logical-job'}).json()
        self.assertTrue(repeated['source']['execution']['replayed'])
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(handler.call_count, 1)

    def test_process_death_releases_the_execution_slot(self):
        marker = self.directory / 'slot-ready'
        root = str(Path(__file__).resolve().parents[1])
        code = ('import sys,time;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
                'from app.executions import ExecutionSlot;'
                'slot=ExecutionSlot(Path(sys.argv[2]));'
                'marker=Path(sys.argv[3]);temporary=marker.with_suffix(".tmp");'
                'temporary.write_text(str(slot.acquire()));temporary.replace(marker);time.sleep(30)')
        child = subprocess.Popen([sys._base_executable, '-c', code, root, str(self.directory), str(marker)])
        slot = ExecutionSlot(self.directory)
        try:
            deadline = time.monotonic()+8
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertEqual(marker.read_text(), 'True')
            self.assertFalse(slot.acquire())
            child.terminate()
            child.wait(timeout=5)
            self.assertTrue(slot.acquire())
        finally:
            slot.release()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


class DeadlineTests(unittest.TestCase):
    def test_phase_and_total_time_limits_are_both_applied(self):
        with patch.dict(os.environ, {'CODEREFEREE_EXECUTION_DEADLINE': '110',
                                    'CODEREFEREE_PHASE_TIMEOUTS': '{"build": 20}'}), \
                patch('scripts.execution_control.time.monotonic', return_value=100):
            self.assertEqual(remaining_timeout(600, 'build'), 10)
            self.assertEqual(remaining_timeout(3, 'clone'), 3)
        with patch.dict(os.environ, {'CODEREFEREE_EXECUTION_DEADLINE': '1'}):
            with self.assertRaises(subprocess.TimeoutExpired):
                remaining_timeout(600)

    def test_invalid_phase_policy_is_a_request_error(self):
        for policy in ({'build': True}, {'build': 0}, {'unknown': 10}):
            with self.assertRaises(ValueError):
                RepositoryValidationRequest(repositoryUrl='https://github.com/example/app', phaseTimeouts=policy)


if __name__ == '__main__':
    unittest.main()
