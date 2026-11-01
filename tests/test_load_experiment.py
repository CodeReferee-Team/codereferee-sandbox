import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_load_experiment as load


def stage(users, requests, failed):
    return {'users': users, 'requests': requests, 'failed_requests': failed,
            'unfinished_users': 0, 'participating_users': min(users, requests)}


class LoadSummaryTests(unittest.TestCase):
    def test_capacity_is_the_last_clean_stage_before_the_first_failing_one(self):
        summary = load.summarize([stage(10, 60, 0), stage(50, 300, 0), stage(100, 600, 12), stage(200, 900, 0)])
        self.assertEqual({k: summary[k] for k in ('max_users_without_errors', 'first_users_with_errors',
                                                  'all_stages_clean')},
                         {'max_users_without_errors': 50, 'first_users_with_errors': 100, 'all_stages_clean': False})

    def test_latency_growth_compares_the_first_and_the_last_answered_stage(self):
        stages = [dict(stage(10, 60, 0), p95_latency_ms=20.0), dict(stage(100, 600, 0), p95_latency_ms=45.0),
                  dict(stage(200, 900, 900), p95_latency_ms=None)]
        self.assertEqual(load.summarize(stages)['latency_growth'],
                         {'from_users': 10, 'from_p95_ms': 20.0, 'to_users': 100, 'to_p95_ms': 45.0, 'ratio': 2.2})
        self.assertIsNone(load.summarize(stages[:1])['latency_growth'])

    def test_overload_behavior_names_how_the_first_failing_stage_failed(self):
        shed = dict(stage(100, 600, 50), status_counts={'200': 550, '503': 40, '429': 10}, error_counts={})
        hung = dict(stage(200, 300, 120), status_counts={'200': 180}, error_counts={'TimeoutError': 100,
                                                                                    'ConnectionResetError': 20})
        self.assertEqual(load.summarize([stage(10, 60, 0), shed, hung])['overload_behavior'],
                         {'users': 100, 'kind': 'rejected', 'rejected': 50, 'timed_out': 0, 'connection_failed': 0})
        self.assertEqual(load.overload_behavior(hung)['kind'], 'timed_out')
        self.assertIsNone(load.summarize([stage(10, 60, 0)])['overload_behavior'])

    def test_a_stage_without_requests_is_not_counted_as_clean(self):
        self.assertEqual(load.summarize([stage(10, 0, 0)])['max_users_without_errors'], None)
        self.assertTrue(load.summarize([stage(10, 20, 0), stage(50, 90, 0)])['all_stages_clean'])

    def test_empty_target_stage_is_not_a_success(self):
        result = load.summarize([stage(10, 60, 0), stage(200, 0, 0)], 200)
        self.assertIsNone(result['target_met'])
        self.assertFalse(result['measurement_valid'])
        self.assertFalse(result['all_stages_clean'])
        self.assertEqual(result['max_users_without_errors'], 10)
        self.assertIn('no_requests', result['measurement_issues'][0]['reasons'])

    def test_unfinished_or_unrepresented_users_cannot_establish_capacity(self):
        for incomplete in (dict(stage(200, 400, 0), unfinished_users=195), stage(200, 5, 0)):
            with self.subTest(incomplete=incomplete):
                result = load.summarize([stage(10, 60, 0), incomplete, stage(300, 600, 0)], 200)
                self.assertIsNone(result['target_met'])
                self.assertFalse(result['measurement_valid'])
                self.assertEqual(result['max_users_without_errors'], 10)

    def test_no_stages_or_missing_expected_stage_is_incomplete(self):
        for stages in ([], [stage(10, 60, 0)], [stage(200, 400, 0), stage(10, 60, 0)]):
            result = load.summarize(stages, 200, expected_stages=[10, 200])
            self.assertFalse(result['measurement_valid'])
            self.assertIsNone(result['target_met'])
        missing_middle = load.summarize([stage(10, 60, 0), stage(200, 400, 0)], 200,
                                       expected_stages=[10, 50, 200])
        self.assertEqual(missing_middle['max_users_without_errors'], 10)

    def test_missing_or_inconsistent_counters_are_not_successful(self):
        for incomplete in ({'users': 200}, dict(stage(200, 400, 0), requests=-1),
                           stage(200, 400, 401)):
            result = load.summarize([incomplete], 200)
            self.assertFalse(result['measurement_valid'])
            self.assertIsNone(result['target_met'])

    def test_completed_target_with_http_failures_is_a_valid_miss(self):
        result = load.summarize([stage(200, 400, 20)], 200, expected_stages=[200])
        self.assertTrue(result['measurement_valid'])
        self.assertFalse(result['target_met'])

    def test_completed_target_without_failures_is_valid(self):
        result = load.summarize([stage(200, 400, 0)], 200, expected_stages=[200])
        self.assertTrue(result['measurement_valid'])
        self.assertTrue(result['target_met'])

    def test_target_users_are_measured_exactly_and_judged_up_to_that_stage(self):
        self.assertEqual(load.with_target([10, 50, 100, 200], 150), [10, 50, 100, 150, 200])
        self.assertEqual(load.with_target([10, 50], None), [10, 50])
        stages = [stage(10, 60, 0), stage(100, 600, 0), stage(150, 900, 30), stage(200, 900, 90)]
        self.assertTrue(load.summarize(stages, 100)['target_met'])
        missed = load.summarize(stages, 150)
        self.assertFalse(missed['target_met'])
        self.assertEqual(missed['target_users'], 150)
        # 기준이 없거나 그 인원을 재지 못했으면 판단하지 않는다.
        self.assertIsNone(load.summarize(stages)['target_met'])
        self.assertIsNone(load.summarize(stages, 120)['target_met'])

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
        self.addCleanup(server.server_close)
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
        self.assertEqual(load.overload_behavior(large)['kind'], 'rejected')
        self.assertIn('503', large['status_counts'])
        (private,) = run('/private', '5')
        self.assertEqual(private['failed_requests'], 0)
        self.assertEqual(set(private['status_counts']), {'401'})
        for result in (small, large, private):
            self.assertEqual(result['participating_users'], result['users'])
            self.assertEqual(result['unfinished_users'], 0)
            self.assertTrue(load.summarize([result])['measurement_valid'])


class GeneratorDeadlineTests(unittest.TestCase):
    def test_unfinished_workers_stop_later_stages_and_cannot_report_success(self):
        # Inject a stuck request into the real generator process; no network/server timing race.
        code = load.GENERATOR_CODE.replace('results=[]',
            'def request():\n time.sleep(30); return 200\nresults=[]')
        env = dict(os.environ, TARGET_URL='http://127.0.0.1/', STAGES='1,2', STAGE_SECONDS='1',
                   THINK_SECONDS='0.01', REQUEST_TIMEOUT='0.1')
        out = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True,
                             text=True, timeout=20, check=True)
        stages = json.loads(out.stdout)['stages']
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]['unfinished_users'], 1)
        self.assertEqual(stages[0]['requests'], 0)
        self.assertEqual(stages[0]['participating_users'], 0)
        summary = load.summarize(stages, 2, expected_stages=[1, 2])
        self.assertFalse(summary['measurement_valid'])
        self.assertIsNone(summary['target_met'])

    def test_stuck_users_share_one_stage_deadline(self):
        """Users that get no answer still finish within one stage deadline, and each stage logs progress."""
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0)); listener.listen(1)  # accepts nothing beyond the backlog
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        env = dict(os.environ, TARGET_URL=f'http://127.0.0.1:{port}/', STAGES='40', STAGE_SECONDS='1',
                   THINK_SECONDS='0.5', REQUEST_TIMEOUT='2')
        started = time.monotonic()
        out = subprocess.run([sys.executable, '-c', load.GENERATOR_CODE], env=env, capture_output=True,
                             text=True, timeout=60)
        took = time.monotonic() - started
        (only,) = json.loads(out.stdout.strip().splitlines()[-1])['stages']
        self.assertEqual(only['users'], 40)
        self.assertLess(took, 15)
        self.assertIn('progress', out.stderr)


class GeneratorStartTests(unittest.TestCase):
    """A generator Pod that never starts must say why, not wait out the whole limit."""

    def test_final_result_can_precede_progress_in_merged_container_logs(self):
        expected = {'stages': [stage(10, 100, 0)]}
        logs = json.dumps({'progress': stage(10, 100, 0)}) + '\n' + json.dumps(expected)
        self.assertEqual(load.generator_result(logs), expected)
        self.assertEqual(load.generator_result(logs + '\n' + json.dumps({'progress': stage(10, 100, 0)})),
                         expected)

    def test_progress_without_a_final_result_is_not_a_complete_measurement(self):
        with self.assertRaisesRegex(RuntimeError, 'no final stage result'):
            load.generator_result(json.dumps({'progress': stage(10, 100, 0)}))

    def pod(self, phase='Pending', waiting=None, scheduled=None):
        status = {'phase': phase}
        if waiting:
            status['containerStatuses'] = [{'state': {'waiting': {'reason': waiting, 'message': 'pull failed'}}}]
        if scheduled:
            status['conditions'] = [{'type': 'PodScheduled', 'status': 'False', 'reason': scheduled,
                                     'message': '0/1 nodes are available: 1 Insufficient cpu.'}]
        return status

    def test_image_pull_failure_is_reported(self):
        reason = load.generator_stuck_reason(self.pod(waiting='ImagePullBackOff'))
        self.assertIn('ImagePullBackOff', reason)
        self.assertIn('python:3.12-alpine', reason)

    def test_unschedulable_is_reported(self):
        self.assertIn('Insufficient cpu', load.generator_stuck_reason(self.pod(scheduled='Unschedulable')))

    def test_pending_while_creating_is_not_stuck(self):
        self.assertIsNone(load.generator_stuck_reason(self.pod(waiting='ContainerCreating')))
        self.assertIsNone(load.generator_stuck_reason(self.pod(phase='Running')))

    def test_run_generator_stops_early_with_the_reason(self):
        args = SimpleNamespace(namespace='ns', stage_seconds=20, load_timeout_seconds=5, think_seconds=1)
        stuck = json.dumps({'status': self.pod(waiting='ErrImagePull')})
        calls = []

        def fake(namespace, *a, input_text=None):
            calls.append(a)
            return SimpleNamespace(stdout=stuck if a[:2] == ('get', 'pod') else '', stderr='', returncode=0)

        with patch.object(load, 'kubectl', side_effect=fake), patch.object(load.subprocess, 'run'), \
             patch.object(load.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'ErrImagePull'):
                load.run_generator(args, 'http://svc', [10])
        self.assertLess(len([c for c in calls if c[:2] == ('get', 'pod')]), 5)


class LoadRunnerTests(unittest.TestCase):
    def arguments(self):
        return SimpleNamespace(namespace='test', deployment='api', service='api', service_port=80,
            label_selector='app=api', probe_path='/', stages='10,50', stage_seconds=1, think_seconds=1.0,
            load_timeout_seconds=5.0, baseline_probes=2, recovery_probes=5, request_timeout_seconds=2,
            recovery_timeout_seconds=1, local_port=18082, target_users=None)

    def probes(self, success=True):
        return [{'at': f'2026-10-30T00:00:0{i}Z', 'success': success, 'status_code': 200 if success else None,
                 'latency_ms': 1} for i in range(5)]

    def run_main(self, recovered, generated=None, target_users=None):
        observer = MagicMock()
        observer.__enter__.return_value = observer
        observer.collect.return_value = self.probes()[:2]
        observer.collect_until_healthy.return_value = (self.probes(recovered), recovered)
        if generated is None:
            generated = {'stages': [dict(stage(10, 100, 0), p95_latency_ms=12), dict(stage(50, 400, 30), p95_latency_ms=900)]}
        args = self.arguments()
        args.target_users = target_users
        output = io.StringIO()
        with patch.object(load, 'parse_args', return_value=args), \
             patch.object(load, 'get_target_configuration', return_value={'replicas': 1}), \
             patch.object(load, 'restart_count', side_effect=[0, 1]), \
             patch.object(load, 'cpu_budget', return_value={'sufficient': True}), \
             patch.object(load, 'InClusterProbe', return_value=observer), \
             patch.object(load, 'run_generator', return_value=generated), contextlib.redirect_stdout(output):
            code = load.main()
        return code, json.loads(output.getvalue())

    def test_target_users_on_too_few_cores_is_not_measured(self):
        args = self.arguments()
        args.target_users = 200
        budget = {'sufficient': False, 'node_allocatable_millicores': 2000, 'app_pods': 2,
                  'app_limit_millicores': 2000, 'other_limit_millicores': 0, 'generator_limit_millicores': 1000}
        output = io.StringIO()
        with patch.object(load, 'parse_args', return_value=args), \
             patch.object(load, 'get_target_configuration', return_value={'replicas': 2}), \
             patch.object(load, 'restart_count', return_value=0), \
             patch.object(load, 'cpu_budget', return_value=budget), \
             patch.object(load, 'InClusterProbe') as probe, \
             patch.object(load, 'run_generator') as generator, contextlib.redirect_stdout(output):
            code = load.main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        probe.assert_not_called()
        generator.assert_not_called()
        self.assertEqual(result['observationStatus'], 'infrastructure_error')
        self.assertEqual(result['infraError'], 'sandbox_cpu_insufficient')
        self.assertIn('3000m', result['stderr'])

    def test_without_target_users_short_cores_are_recorded_not_blocking(self):
        observer = MagicMock()
        observer.__enter__.return_value = observer
        observer.collect.return_value = self.probes()[:2]
        observer.collect_until_healthy.return_value = (self.probes(), True)
        output = io.StringIO()
        with patch.object(load, 'parse_args', return_value=self.arguments()), \
             patch.object(load, 'get_target_configuration', return_value={'replicas': 2}), \
             patch.object(load, 'restart_count', return_value=0), \
             patch.object(load, 'cpu_budget', return_value={'sufficient': False}), \
             patch.object(load, 'InClusterProbe', return_value=observer), \
             patch.object(load, 'run_generator', return_value={'stages': [stage(10, 100, 0), stage(50, 100, 0)]}), \
             contextlib.redirect_stdout(output):
            self.assertEqual(load.main(), 0)
        self.assertEqual(json.loads(output.getvalue())['chaos_observation']['load']['cpu_budget'],
                         {'sufficient': False})

    def test_result_carries_capacity_and_crash_evidence(self):
        code, result = self.run_main(recovered=True)
        self.assertEqual(code, 0)
        observation = result['chaos_observation']
        self.assertEqual(observation['type'], 'load_concurrent_users')
        self.assertEqual(observation['load']['max_users_without_errors'], 10)
        self.assertEqual(observation['load']['first_users_with_errors'], 50)
        self.assertEqual(observation['restart_count_delta'], 1)
        self.assertEqual(observation['load']['cpu_budget'], {'sufficient': True})
        self.assertEqual(observation['recovery_seconds'], 0.0)
        # 부하 요청 수는 묶음 가용성(probe_count 합)에 섞지 않는다.
        self.assertEqual(result['metrics']['probe_count'], 7)

    def test_service_that_does_not_answer_after_the_load_did_not_recover(self):
        code, result = self.run_main(recovered=False)
        self.assertEqual(code, 1)
        self.assertFalse(result['chaos_observation']['recovered'])

    def test_incomplete_measurement_is_infra_error_even_if_app_recovers(self):
        for target in (None, 50):
            for stages in ([stage(10, 100, 0), stage(50, 0, 0)],
                           [stage(10, 100, 0), dict(stage(50, 100, 0), unfinished_users=3)],
                           [stage(10, 100, 0)], []):
                with self.subTest(target=target, stages=stages):
                    code, result = self.run_main(True, {'stages': stages}, target)
                    self.assertEqual(code, 1)
                    self.assertIsNone(result['exitCode'])
                    self.assertEqual(result['observationStatus'], 'infrastructure_error')
                    self.assertEqual(result['infraError'], 'load_measurement_incomplete')
                    self.assertFalse(result['timedOut'])
                    self.assertTrue(result['chaos_observation']['recovered'])
                    self.assertIsNone(result['chaos_observation']['load']['target_met'])
                    self.assertEqual(result['chaos_observation']['load']['stages'], stages)

    def test_http_adapter_preserves_incomplete_measurement_as_infra(self):
        from app.main import parse_experiment_result
        code, result = self.run_main(True, {'stages': [stage(10, 100, 0), stage(50, 0, 0)]}, 50)
        parsed = parse_experiment_result(subprocess.CompletedProcess([], code, json.dumps(result), ''))
        self.assertIsNone(parsed['exitCode'])
        self.assertEqual(parsed['observationStatus'], 'infrastructure_error')
        self.assertEqual(parsed['infraError'], 'load_measurement_incomplete')


def pod(name, cpu, phase='Running'):
    resources = {'limits': {'cpu': cpu}} if cpu else {}
    return {'metadata': {'name': name}, 'status': {'phase': phase},
            'spec': {'containers': [{'name': 'c', 'resources': resources}]}}


class CpuBudgetTests(unittest.TestCase):
    """2026-10-30: on a 2 vCPU node petclinic got worse from 1 to 2 replicas."""

    def budget(self, node_cpu, app, others=()):
        nodes = {'items': [{'status': {'allocatable': {'cpu': node_cpu}}}]}

        def run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, json.dumps(nodes), '')

        def kubectl(_namespace, *args, **_kwargs):
            items = app if '-l' in args else [*app, *others]
            return subprocess.CompletedProcess(args, 0, json.dumps({'items': items}), '')
        with patch.object(load.subprocess, 'run', side_effect=run), patch.object(load, 'kubectl', side_effect=kubectl):
            return load.cpu_budget('test', 'app=api')

    def test_one_replica_fits_two_cores(self):
        report = self.budget('2', [pod('api-1', '1000m')])
        self.assertTrue(report['sufficient'])
        self.assertEqual(report['app_limit_millicores'], 1000)
        self.assertEqual(report['generator_limit_millicores'], 1000)

    def test_two_replicas_do_not_fit_two_cores(self):
        report = self.budget('2', [pod('api-1', '1000m'), pod('api-2', '1000m')])
        self.assertFalse(report['sufficient'])
        self.assertEqual(report['app_pods'], 2)

    def test_local_database_counts_against_the_same_cores(self):
        report = self.budget('2', [pod('api-1', '1000m')], [pod('local-mysql', '500m')])
        self.assertEqual(report['other_limit_millicores'], 500)
        self.assertFalse(report['sufficient'])
        finished = self.budget('2', [pod('api-1', '1000m')], [pod('old', '500m', phase='Succeeded')])
        self.assertTrue(finished['sufficient'])

    def test_unknown_when_the_app_has_no_cpu_limit(self):
        report = self.budget('4', [pod('api-1', None)])
        self.assertIsNone(report['sufficient'])

    def test_millicores(self):
        self.assertEqual(load.millicores('1500m'), 1500)
        self.assertEqual(load.millicores('2'), 2000)
        self.assertEqual(load.millicores('0.5'), 500)
        self.assertIsNone(load.millicores(None))


if __name__ == '__main__':
    unittest.main()
