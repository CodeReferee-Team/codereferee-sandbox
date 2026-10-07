import os
import unittest
from unittest.mock import patch
from scripts.metrics_lifecycle import RequestMetrics, receiver_url


TARGET = {'namespace': 'codereferee-test', 'deployment': 'api', 'labelSelector': 'app=api'}


class MetricsLifecycleTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_disabled_preserves_existing_execution(self):
        session = RequestMetrics('test', TARGET)
        session.start()
        self.assertEqual(session.finish()['status'], 'disabled')
        self.assertFalse(session.attempted)

    @patch.dict(os.environ, {'CODEREFEREE_METRICS_REMOTE_WRITE_URL': 'http://receiver/write',
                           'CODEREFEREE_METRICS_QUERY_URL': 'http://receiver'}, clear=True)
    @patch('scripts.metrics_lifecycle.wiring.uninstall')
    @patch('scripts.metrics_lifecycle.wiring.apply', side_effect=RuntimeError('apply failed'))
    def test_partial_install_is_cleaned_without_service_verdict(self, apply, uninstall):
        session = RequestMetrics('test', TARGET)
        session.start()
        result = session.finish()
        self.assertEqual(result['status'], 'error')
        self.assertTrue(result['cleanup']['removed'])
        self.assertNotIn('exitCode', result)
        uninstall.assert_called_once_with(session.namespace)

    def test_query_labels_and_receiver_validation(self):
        selector = RequestMetrics('test', TARGET).selector('container_cpu_usage_seconds_total')
        self.assertIn('namespace="codereferee-test"', selector)
        self.assertIn('codereferee_request_id="test"', selector)
        for url in ('file:///tmp', 'http://secret:password@host', 'http://host/?token=x'):
            with self.assertRaises(ValueError):
                receiver_url(url)

    @patch.dict(os.environ, {}, clear=True)
    def test_source_timestamp_not_query_evaluation_time_is_used(self):
        session = RequestMetrics('test', TARGET)
        with patch.object(session, 'query', return_value=[{'value': [9000, '12']}]) as query:
            self.assertEqual(session.latest_sample('container_cpu_usage_seconds_total'), 12)
        self.assertIn('timestamp(', query.call_args.args[0])

    @patch.dict(os.environ, {'CODEREFEREE_METRICS_REMOTE_WRITE_URL': 'http://receiver/write',
                           'CODEREFEREE_METRICS_QUERY_URL': 'http://receiver'}, clear=True)
    @patch('scripts.metrics_lifecycle.wiring.uninstall', side_effect=RuntimeError('cleanup blocked'))
    def test_cleanup_failure_reported_without_changing_application_result(self, uninstall):
        session = RequestMetrics('test', TARGET)
        session.attempted = True
        session.report['status'] = 'error'
        report = session.finish()
        self.assertFalse(report['cleanup']['removed'])
        self.assertIn('Observation namespace cleanup failed.', report['errors'])
        self.assertNotIn('exitCode', report)

    @patch.dict(os.environ, {}, clear=True)
    @patch('scripts.metrics_lifecycle.wiring.run')
    def test_replacement_uid_is_preserved_alongside_original(self, run):
        import json
        import subprocess
        session = RequestMetrics('test', TARGET)
        for name, uid in [('api-old', 'uid-old'), ('api-new', 'uid-new')]:
            pod = {'metadata': {'name': name, 'uid': uid},
                   'spec': {'containers': [{'name': 'api', 'resources': {'limits': {'memory': '128Mi'}}}]},
                   'status': {'containerStatuses': [{'name': 'api', 'restartCount': 0}]}}
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps({'items': [pod]}), '')
            session.snapshot()
        self.assertEqual({entry['uid'] for entry in session.bindings.values()}, {'uid-old', 'uid-new'})
        selector = session.selector('container_memory_working_set_bytes')
        self.assertIn('pod=~', selector)
        self.assertIn('api', selector)
        self.assertIn('old', selector)
        self.assertIn('new', selector)


if __name__ == '__main__':
    unittest.main()
