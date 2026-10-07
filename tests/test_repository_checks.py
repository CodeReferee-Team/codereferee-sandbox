import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from execution_plan import ConfigurationRequired
from repository_checks import CheckFailure, check_plan, verify_repository


class RepositoryChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repository'
        self.root.mkdir()
        self.cache = Path(self.temp.name) / 'cache'

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def node(self, scripts):
        self.write('package.json', json.dumps({'scripts': scripts}))

    @patch('repository_checks.subprocess.run')
    def test_node_without_tests_is_not_reported_as_test_pass(self, run):
        self.node({'start': 'node app.js'})
        run.return_value = subprocess.CompletedProcess([], 0, '', '')
        report = verify_repository(self.root, self.cache, 'test')
        self.assertEqual(report['test_execution'], 'not_declared')
        self.assertFalse(report['verification_declared'])
        self.assertEqual([s['name'] for s in report['steps']], ['install'])
        command = next(c.args[0] for c in run.call_args_list if c.args[0][:2] == ['docker', 'run'])
        self.assertIn('--cap-drop', command)
        self.assertIn('2g', command)
        self.assertNotIn('docker.sock', ' '.join(command))

    @patch('repository_checks.subprocess.run')
    def test_actual_test_failure_preserves_stage_and_exit_code(self, run):
        self.node({'test': 'node --test'})
        run.side_effect = [subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 3, 'test output', 'assertion failed'),
            subprocess.CompletedProcess([], 0, '', '')]
        with self.assertRaises(CheckFailure) as failed:
            verify_repository(self.root, self.cache, 'test')
        self.assertEqual(failed.exception.report['exit_code'], 3)
        self.assertEqual(failed.exception.report['failed_step'], 'test')
        self.assertFalse(failed.exception.report['infrastructure_error'])

    def test_explicit_verification_uses_same_yaml_and_does_not_import_host_env(self):
        self.write('gradlew', 'wrapper')
        self.write('.codereferee/validation.yaml', 'verification:\n  stack: gradle\n  test: [sh, ./gradlew, test]\n  env: {SPRING_PROFILES_ACTIVE: test}\n')
        plan = check_plan(self.root)
        self.assertTrue(plan['declared'])
        self.assertEqual(plan['env'], {'SPRING_PROFILES_ACTIVE': 'test'})
        self.write('.codereferee/validation.yaml', 'verification:\n  env: {TOKEN: "${TOKEN}"}\n')
        with self.assertRaises(ConfigurationRequired): check_plan(self.root)

    @patch('repository_checks.subprocess.run')
    def test_timeout_removes_only_its_own_check_container(self, run):
        self.node({})
        run.side_effect = [subprocess.CompletedProcess([], 0, '', ''),
            subprocess.TimeoutExpired('docker', 1), subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, '', '')]
        with self.assertRaises(CheckFailure) as failure:
            verify_repository(self.root, self.cache, 'test', timeout=1)
        self.assertTrue(failure.exception.report['timed_out'])
        self.assertTrue(failure.exception.report['check_container_cleanup']['removed'])
        original = run.call_args_list[1].args[0]
        cleanup = run.call_args_list[2].args[0]
        self.assertEqual(cleanup, ['docker', 'rm', '--force', original[original.index('--name')+1]])
        self.assertTrue(failure.exception.report['check_cache_cleanup']['removed'])

    @patch('repository_checks.subprocess.run')
    def test_gradle_no_source_is_not_a_successful_test_suite(self, run):
        self.write('gradlew', 'wrapper')
        self.write('src/test/placeholder', '')
        run.return_value = subprocess.CompletedProcess([], 0, 'NO-SOURCE', '')
        report = verify_repository(self.root, self.cache, 'test')
        self.assertEqual(report['test_execution'], 'no_tests_collected')
        self.assertEqual(report['test_count'], 0)

    def test_ambiguous_stack_requires_explicit_choice(self):
        self.node({})
        self.write('requirements.txt', '')
        with self.assertRaises(ConfigurationRequired): check_plan(self.root)

    def test_shell_expression_is_not_executed_on_the_host(self):
        self.node({})
        self.write('.codereferee/validation.yaml', 'test: "npm test; echo bypass"\n')
        with self.assertRaises(ConfigurationRequired): check_plan(self.root)

    def test_multimodule_tests_are_not_missed_when_root_has_no_src_test(self):
        self.write('gradlew', 'wrapper')
        self.write('modules/api/src/test/java/ExampleTest.java', 'test')
        self.assertEqual(check_plan(self.root)['test'], ['sh', './gradlew', 'test', '--no-daemon'])
