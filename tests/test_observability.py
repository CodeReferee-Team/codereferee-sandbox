import sys
import unittest
from pathlib import Path
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install_observability as wiring


class ObservabilityTests(unittest.TestCase):
    def test_request_namespace_and_container_filter(self):
        text = wiring.render('http://receiver:9090/api/v1/write', 'run-1', 'local', 'codereferee-run-1')
        documents = list(yaml.safe_load_all(text))
        namespace = wiring.observation_namespace('run-1', 'codereferee-run-1')
        self.assertEqual(documents[0]['metadata']['name'], namespace)
        for obj in documents:
            if 'namespace' in obj['metadata']:
                self.assertEqual(obj['metadata']['namespace'], namespace)
        config = yaml.safe_load(next(o for o in documents if o['kind'] == 'ConfigMap')['data']['prometheus.yml'])
        for scrape in config['scrape_configs']:
            self.assertEqual(scrape['kubernetes_sd_configs'][0]['namespaces']['names'], [namespace])
        self.assertIn('codereferee-run-1', str(config['scrape_configs'][0]['metric_relabel_configs']))
        self.assertNotIn('${', str(documents))
        self.assertNotEqual(namespace, wiring.observation_namespace('run-2', 'codereferee-run-2'))

    def test_rejects_unsafe_receiver_and_cleanup(self):
        for url in ('file:///tmp/a', 'http://user:password@receiver/write', 'http://receiver/write"\ninvalid'):
            with self.assertRaises(ValueError):
                wiring.render(url, 'run-1', 'local')
        with self.assertRaises(ValueError):
            wiring.uninstall('kube-system')


if __name__ == '__main__':
    unittest.main()
