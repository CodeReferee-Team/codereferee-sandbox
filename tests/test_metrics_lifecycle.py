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


if __name__ == '__main__':
    unittest.main()
