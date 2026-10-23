import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from execution_plan import ConfigurationRequired
import repository_checks
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
        command = next(c.args[0] for c in run.call_args_list if '--user' in c.args[0])
        self.assertIn('--cap-drop', command)
        # root 가 아닌 사용자로 돈다는 것이 요지다. 값 자체는 호스트에 따라 다르다.
        self.assertEqual(repository_checks.CHECK_OWNER or '1000:1000',
                         command[command.index('--user') + 1])
        self.assertNotIn('--cap-add', command)
        self.assertIn('2g', command)
        self.assertNotIn('docker.sock', ' '.join(command))
        self.assertIn('readonly', ' '.join(command))
        setup = next(c.args[0] for c in run.call_args_list if '--cap-add' in c.args[0])
        self.assertIn('none', setup)
        self.assertNotIn('type=bind', ' '.join(setup))

    @patch('repository_checks.subprocess.run')
    def test_jvm_checks_run_as_the_host_user_so_the_source_mount_is_writable(self, run):
        """cap-drop ALL 은 컨테이너 root 의 CAP_DAC_OVERRIDE 도 뗀다.

        호스트 사용자 소유의 레포를 bind 로 붙이면 uid 가 다른 순간 소스 트리에
        쓰지 못한다. Gradle 은 GRADLE_USER_HOME 과 별개로 프로젝트 안에 .gradle 을
        만들기 때문에 여기서 막혔다. Docker Desktop 은 소유권을 가상화해서 이
        조건이 드러나지 않으므로 네이티브 Linux 에서만 나는 실패였다.
        """
        self.write('build.gradle', 'plugins { id "java" }')
        self.write('gradlew', '#!/bin/sh\n')
        run.return_value = subprocess.CompletedProcess([], 0, '', '')
        verify_repository(self.root, self.cache, 'test')

        step = next(c.args[0] for c in run.call_args_list if '--workdir' in c.args[0])
        self.assertIn('target=/workspace', ' '.join(step))
        if repository_checks.CHECK_OWNER:
            self.assertEqual(repository_checks.CHECK_OWNER, step[step.index('--user') + 1])
            # 이미지에 없는 uid 로 돌면 HOME 이 쓰기 불가가 된다.
            self.assertIn('HOME=/cache/home', step)
        else:
            self.assertNotIn('--user', step)

    @patch('repository_checks.subprocess.run')
    def test_cache_volume_is_owned_by_the_user_that_runs_the_checks(self, run):
        self.write('build.gradle', 'plugins { id "java" }')
        self.write('gradlew', '#!/bin/sh\n')
        run.return_value = subprocess.CompletedProcess([], 0, '', '')
        verify_repository(self.root, self.cache, 'test')

        setup = next(c.args[0] for c in run.call_args_list if '--cap-add' in c.args[0])
        # 준비 컨테이너만 CHOWN 을 받는다. 제출된 코드는 받지 않는다.
        self.assertEqual(['CHOWN'], [setup[index + 1] for index, item in enumerate(setup) if item == '--cap-add'])
        self.assertNotIn('type=bind', ' '.join(setup))
        self.assertIn('chown ' + (repository_checks.CHECK_OWNER or '1000:1000'), setup[-1])
        for directory in ('/cache/home', '/cache/gradle', '/cache/python'):
            self.assertIn(directory, setup[-1])

    @patch('repository_checks.subprocess.run')
    def test_unwritable_repository_is_reported_as_infrastructure_not_a_repo_failure(self, run):
        """우리 쪽 권한 문제가 사용자 코드 결함으로 나가면 안 된다."""
        if not repository_checks.CHECK_OWNER:
            self.skipTest('POSIX 권한이 있는 환경에서만 의미가 있다')
        self.write('build.gradle', 'plugins { id "java" }')
        self.write('gradlew', '#!/bin/sh\n')
        run.return_value = subprocess.CompletedProcess([], 0, '', '')
        with patch('repository_checks.os.access', return_value=False):
            with self.assertRaises(CheckFailure) as raised:
                verify_repository(self.root, self.cache, 'test')
        report = raised.exception.report
        self.assertTrue(report['infrastructure_error'])
        self.assertEqual('prepare', report['failed_step'])
        self.assertEqual('not_attempted', report['test_execution'])

    @patch('repository_checks.subprocess.run')
    def test_actual_test_failure_preserves_stage_and_exit_code(self, run):
        self.node({'test': 'node --test'})
        run.side_effect = [subprocess.CompletedProcess([], 0, '', ''), subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 3, 'test output', 'assertion failed'),
            subprocess.CompletedProcess([], 0, '', '')]
        with self.assertRaises(CheckFailure) as failed:
            verify_repository(self.root, self.cache, 'test')
        self.assertEqual(failed.exception.report['exit_code'], 3)
        self.assertEqual(failed.exception.report['failed_step'], 'test')
        self.assertFalse(failed.exception.report['infrastructure_error'])

    def install_failure(self, stderr):
        self.node({'test': 'node --test'})
        ok = subprocess.CompletedProcess([], 0, '', '')
        repository_checks.subprocess.run.side_effect = [ok, ok, subprocess.CompletedProcess([], 1, '', stderr), ok, ok]
        with self.assertRaises(CheckFailure) as failed:
            verify_repository(self.root, self.cache, 'test')
        return failed.exception.report

    @patch('repository_checks.subprocess.run')
    def test_registry_unreachable_during_install_is_infrastructure(self, run):
        # 패키지 저장소에 닿지 못한 것은 레포 결함이 아니다. FAILED로 내보내면 멀쩡한 코드가 불합격이 된다.
        for stderr in ('npm ERR! code EAI_AGAIN\nnpm ERR! request to https://registry.npmjs.org/x failed',
                       "Retrying (Retry(total=4)) after connection broken by 'SSLError(SSLCertVerificationError(1, "
                       "'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed'))'",
                       'toomanyrequests: You have reached your unauthenticated pull rate limit.'):
            with self.subTest(stderr=stderr[:30]):
                report = self.install_failure(stderr)
                self.assertTrue(report['infrastructure_error'])
                self.assertEqual('install', report['failed_step'])

    @patch('repository_checks.subprocess.run')
    def test_missing_package_during_install_stays_a_repository_failure(self, run):
        report = self.install_failure('npm ERR! code E404\nnpm ERR! 404 Not Found - GET https://registry.npmjs.org/no-such-pkg')
        self.assertFalse(report['infrastructure_error'])
        self.assertEqual(1, report['exit_code'])

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
        run.side_effect = [subprocess.CompletedProcess([], 0, '', ''), subprocess.CompletedProcess([], 0, '', ''),
            subprocess.TimeoutExpired('docker', 1), subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, '', '')]
        with self.assertRaises(CheckFailure) as failure:
            verify_repository(self.root, self.cache, 'test', timeout=1)
        self.assertTrue(failure.exception.report['timed_out'])
        self.assertTrue(failure.exception.report['check_container_cleanup']['removed'])
        original = run.call_args_list[2].args[0]
        cleanup = run.call_args_list[3].args[0]
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

    @patch('repository_checks.subprocess.run')
    def test_node_setup_failure_is_infrastructure_and_cache_is_removed(self, run):
        self.node({})
        run.side_effect = [subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 125, '', 'volume setup failed'),
            subprocess.CompletedProcess([], 0, '', '')]
        with self.assertRaises(CheckFailure) as failure:
            verify_repository(self.root, self.cache, 'test')
        self.assertIsNone(failure.exception.report['exit_code'])
        self.assertTrue(failure.exception.report['infrastructure_error'])
        self.assertTrue(failure.exception.report['check_cache_cleanup']['removed'])
