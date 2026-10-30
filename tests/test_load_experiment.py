import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_load_experiment as load


def stage(users, requests, failed):
    return {'users': users, 'requests': requests, 'failed_requests': failed}


class LoadSummaryTests(unittest.TestCase):
    def test_capacity_is_the_last_clean_stage_before_the_first_failing_one(self):
        summary = load.summarize([stage(10, 60, 0), stage(50, 300, 0), stage(100, 600, 12), stage(200, 900, 0)])
        self.assertEqual(summary, {'max_users_without_errors': 50, 'first_users_with_errors': 100,
                                   'all_stages_clean': False})

    def test_a_stage_without_requests_is_not_counted_as_clean(self):
        self.assertEqual(load.summarize([stage(10, 0, 0)])['max_users_without_errors'], None)
        self.assertTrue(load.summarize([stage(10, 5, 0), stage(50, 9, 0)])['all_stages_clean'])

    def test_stages_must_increase(self):
        self.assertEqual(load.parse_stages('10,50,100'), [10, 50, 100])
        for value in ('50,10', '0,10', '10,10', '', '10,600'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                load.parse_stages(value)


class GeneratorTests(unittest.TestCase):
    """Runs the code that the generator Pod runs against a local server with a capacity limit."""

    def test_overload_is_counted_as_failures_and_auth_errors_are_not(self):
        gate = threading.Semaphore(3)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/private':
                    self.send_response(401); self.end_headers(); return
                if not gate.acquire(timeout=0.05):
                    self.send_response(503); self.end_headers(); return
                try:
                    threading.Event().wait(0.1)
                    self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
                finally:
                    gate.release()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        def run(path, stages):
            env = dict(os.environ, TARGET_URL=f'http://127.0.0.1:{port}{path}', STAGES=stages,
                       STAGE_SECONDS='2', THINK_SECONDS='0.5', REQUEST_TIMEOUT='2')
            out = subprocess.run([sys.executable, '-c', load.GENERATOR_CODE], env=env, capture_output=True,
                                 text=True, timeout=60)
            return json.loads(out.stdout)['stages']

        small, large = run('/', '1,60')
        self.assertEqual(small['failed_requests'], 0)
        self.assertGreater(small['requests'], 0)
        self.assertGreater(large['failed_requests'], 0)
        self.assertIn('503', large['status_counts'])
        (private,) = run('/private', '5')
        self.assertEqual(private['failed_requests'], 0)
        self.assertEqual(set(private['status_counts']), {'401'})


class LoadRunnerTests(unittest.TestCase):
    def arguments(self):
        return SimpleNamespace(namespace='test', deployment='api', service='api', service_port=80,
            label_selector='app=api', probe_path='/', stages='10,50', stage_seconds=1, think_seconds=1.0,
            load_timeout_seconds=5.0, baseline_probes=2, recovery_probes=5, request_timeout_seconds=2,
            recovery_timeout_seconds=1, local_port=18082)

    def probes(self, success=True):
        return [{'at': f'2026-10-30T00:00:0{i}Z', 'success': success, 'status_code': 200 if success else None,
                 'latency_ms': 1} for i in range(5)]

    def run_main(self, recovered):
        observer = MagicMock()
        observer.__enter__.return_value = observer
        observer.collect.return_value = self.probes()[:2]
        observer.collect_until_healthy.return_value = (self.probes(recovered), recovered)
        generated = {'stages': [dict(stage(10, 100, 0), p95_latency_ms=12), dict(stage(50, 400, 30), p95_latency_ms=900)]}
        output = io.StringIO()
        with patch.object(load, 'parse_args', return_value=self.arguments()), \
             patch.object(load, 'get_target_configuration', return_value={'replicas': 1}), \
             patch.object(load, 'restart_count', side_effect=[0, 1]), \
             patch.object(load, 'InClusterProbe', return_value=observer), \
             patch.object(load, 'run_generator', return_value=generated), contextlib.redirect_stdout(output):
            code = load.main()
        return code, json.loads(output.getvalue())

    def test_result_carries_capacity_and_crash_evidence(self):
        code, result = self.run_main(recovered=True)
        self.assertEqual(code, 0)
        observation = result['chaos_observation']
        self.assertEqual(observation['type'], 'load_concurrent_users')
        self.assertEqual(observation['load']['max_users_without_errors'], 10)
        self.assertEqual(observation['load']['first_users_with_errors'], 50)
        self.assertEqual(observation['restart_count_delta'], 1)
        self.assertEqual(observation['recovery_seconds'], 0.0)
        # 부하 요청 수는 묶음 가용성(probe_count 합)에 섞지 않는다.
        self.assertEqual(result['metrics']['probe_count'], 7)

    def test_service_that_does_not_answer_after_the_load_did_not_recover(self):
        code, result = self.run_main(recovered=False)
        self.assertEqual(code, 1)
        self.assertFalse(result['chaos_observation']['recovered'])


if __name__ == '__main__':
    unittest.main()
