import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from artifact_cleanup import cleanup_request_image


class ArtifactCleanupTests(unittest.TestCase):
    @patch('artifact_cleanup.subprocess.run')
    def test_only_matching_request_image_is_removed_without_prune_or_force(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, '', '')
        with patch.dict(os.environ, {'CODEREFEREE_CLUSTER_PROVIDER': 'existing'}):
            result = cleanup_request_image('codereferee/job:abcdef123456', 'codereferee-job')
        self.assertTrue(result['host_removed'])
        self.assertEqual(run.call_args.args[0], ['docker', 'image', 'rm', 'codereferee/job:abcdef123456'])

    @patch('artifact_cleanup.subprocess.run')
    def test_failed_build_image_already_absent_is_clean_not_an_error(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, '', 'Error response from daemon: No such image: codereferee/job:abcdef123456')
        with patch.dict(os.environ, {'CODEREFEREE_CLUSTER_PROVIDER': 'existing'}):
            result = cleanup_request_image('codereferee/job:abcdef123456', 'codereferee-job')
        self.assertTrue(result['host_removed'])
        self.assertTrue(result['host_already_absent'])
        self.assertEqual(result['errors'], [])

    @patch('artifact_cleanup.subprocess.run')
    def test_docker_daemon_failure_is_not_reported_as_absent(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, '', 'Cannot connect to the Docker daemon')
        with patch.dict(os.environ, {'CODEREFEREE_CLUSTER_PROVIDER': 'existing'}):
            result = cleanup_request_image('codereferee/job:abcdef123456', 'codereferee-job')
        self.assertFalse(result['host_removed'])
        self.assertEqual(len(result['errors']), 1)

    @patch('artifact_cleanup.subprocess.run')
    def test_unrelated_image_or_namespace_is_rejected_before_mutation(self, run):
        for image, namespace in [('postgres:17', 'codereferee-job'),
                                 ('codereferee/job:abcdef123456', 'codereferee-other')]:
            with self.assertRaises(ValueError):
                cleanup_request_image(image, namespace)
        run.assert_not_called()
