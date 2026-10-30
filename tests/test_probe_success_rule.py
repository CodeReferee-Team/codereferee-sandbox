import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collect_baseline import answered
from execution_plan import render_plan, resolve_plan
from in_cluster_probe import OBSERVER_CODE, InClusterProbe
from app.main import ChaosTarget, probe_environment


class ProbeSuccessRuleTests(unittest.TestCase):
    """GET /가 401·404인 앱도 서버는 떠 있다. 3차 실측 6건(Spring Security 401, 루트 라우트 없는 API 404)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'package.json').write_text(json.dumps({'scripts': {'start': 'node app.js'}}))

    def api(self, plan):
        manifest, target = render_plan(plan, 'codereferee-test', 'codereferee/test:abc')
        deployment = next(o for o in yaml.safe_load_all(manifest) if o['kind'] == 'Deployment')
        return deployment['spec']['template']['spec']['containers'][0], target

    def test_undeclared_health_path_starts_on_open_port_and_counts_client_errors_as_answers(self):
        container, target = self.api(resolve_plan(self.root))
        self.assertEqual(container['startupProbe']['tcpSocket'], {'port': 3000})
        self.assertNotIn('httpGet', container['readinessProbe'])
        self.assertTrue(target['probeAcceptsClientErrors'])
        self.assertTrue(ChaosTarget.model_validate(dict(target, namespace='n')).probe_accepts_client_errors)

    def test_declared_health_path_keeps_the_strict_http_check(self):
        (self.root / '.codereferee').mkdir()
        (self.root / '.codereferee/validation.yaml').write_text('version: 1\nservice: {port: 3000, healthPath: /health}\n')
        container, target = self.api(resolve_plan(self.root))
        self.assertEqual(container['startupProbe']['httpGet']['path'], '/health')
        self.assertFalse(target['probeAcceptsClientErrors'])

    def test_answered_follows_the_request_rule_and_never_accepts_5xx_or_no_answer(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CODEREFEREE_PROBE_SUCCESS_BELOW', None)
            self.assertEqual([answered(c) for c in (200, 302, 404, 500, None)], [True, True, False, False, False])
        with patch.dict(os.environ, {'CODEREFEREE_PROBE_SUCCESS_BELOW': '500'}):
            self.assertEqual([answered(c) for c in (200, 401, 404, 429, 500, 503, None)],
                             [True, True, True, True, False, False, False])
            self.assertIn('SUCCESS_BELOW', OBSERVER_CODE)
        self.assertIsNone(probe_environment(ChaosTarget.model_validate({'namespace': 'n', 'deployment': 'd',
            'service': 's', 'servicePort': 80, 'labelSelector': 'a=b'})))

    def test_observer_pod_receives_the_rule(self):
        applied = []
        with patch.dict(os.environ, {'CODEREFEREE_PROBE_SUCCESS_BELOW': '500'}), \
                patch.object(InClusterProbe, '__exit__', return_value=None), \
                patch.object(InClusterProbe, 'run', side_effect=lambda *a, input_text=None: applied.append(input_text) or (_ for _ in ()).throw(RuntimeError('stop'))):
            with self.assertRaises(RuntimeError):
                InClusterProbe('n', 'svc', 80).__enter__()
        env = {e['name']: e['value'] for e in json.loads(applied[0])['spec']['containers'][0]['env']}
        self.assertEqual(env['PROBE_SUCCESS_BELOW'], '500')


if __name__ == '__main__':
    unittest.main()
