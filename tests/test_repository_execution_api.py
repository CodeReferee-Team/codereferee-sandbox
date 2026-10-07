import json
import subprocess
import unittest
from unittest.mock import patch

from app.main import RepositoryValidationRequest, RepositoryExecutionFailure, attach_repository_checks, deploy_repository, validate_repository


class RepositoryExecutionApiTests(unittest.TestCase):
    def test_repository_checks_are_preserved_without_inventing_service_results(self):
        result = {'sandboxReport': {'service_smoke_observed': True, 'test_execution': 'not_attempted'}}
        attach_repository_checks(result, {'sandboxReport': {'test_execution': 'not_declared',
            'detected_stack': 'node', 'verification_declared': False, 'steps': [], 'outcome': 'passed'}})
        self.assertEqual(result['sandboxReport']['test_execution'], 'not_declared')
        self.assertTrue(result['sandboxReport']['service_smoke_observed'])
        self.assertNotIn('serverStarted', result)

    @patch('app.main.subprocess.run')
    def test_missing_profile_is_omitted_to_enable_repository_discovery(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps({'target': {}}), '')
        deploy_repository(RepositoryValidationRequest(repositoryUrl='https://github.com/test/repo',
                          requestId='test', chaosMode='suite_quick'))
        self.assertNotIn('--profile', run.call_args.args[0])

    @patch('app.main.subprocess.run')
    def test_build_failure_is_not_reclassified_as_infrastructure(self, run):
        expected = {'exitCode': 1, 'observationStatus': 'observed',
                    'sandboxReport': {'failed_step': 'build'}}
        run.return_value = subprocess.CompletedProcess([], 1, json.dumps({'executionFailure': True, 'result': expected}), '')
        with self.assertRaises(RepositoryExecutionFailure) as failure:
            deploy_repository(RepositoryValidationRequest(repositoryUrl='https://github.com/test/repo',
                              requestId='test', chaosMode='suite_quick'))
        self.assertEqual(failure.exception.result, expected)

    @patch('app.main.cleanup_namespace')
    @patch('app.main.subprocess.run')
    @patch('app.main.deploy_repository')
    def test_no_chaos_request_observes_actual_repository_not_fixture(self, deploy, run, cleanup):
        deploy.return_value = {'repository': {'commitSha': 'sha'}, 'target': {
            'namespace': 'codereferee-test', 'deployment': 'api', 'service': 'api',
            'servicePort': 80, 'labelSelector': 'app=api', 'probePath': '/health'}}
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps({'exitCode': 0, 'observationStatus': 'observed'}), '')
        result = validate_repository(RepositoryValidationRequest(repositoryUrl='https://github.com/test/repo', requestId='test'))
        self.assertIn('run_service_smoke.py', run.call_args.args[0][1])
        self.assertIn('/health', run.call_args.args[0])
        self.assertEqual(result['commitSha'], 'sha')
        cleanup.assert_called_once_with('codereferee-test')
