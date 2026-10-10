import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from pod_diagnostics import manifest_redaction_values, redact, collect_pod_diagnostics
from deploy_repository import render_template


class ManifestRedactionTests(unittest.TestCase):
    def manifest(self, secret=None, env=None, env_from=None, init=False, namespace='request-ns'):
        container = {'name': 'api', 'env': env or [], 'envFrom': env_from or []}
        deployment = {'kind': 'Deployment', 'metadata': {'namespace': 'request-ns'},
            'spec': {'template': {'spec': {'initContainers' if init else 'containers': [container]}}}}
        documents = [deployment]
        if secret is not None:
            documents.append({'kind': 'Secret', 'metadata': {'name': 'credentials', 'namespace': namespace}, **secret})
        return yaml.safe_dump_all(documents)

    def reference(self, key='odd-name'):
        # Neither the env name nor the Secret key hints that the value is sensitive.
        return [{'name': 'SETTING', 'valueFrom': {'secretKeyRef': {'name': 'credentials', 'key': key}}}]

    def test_secret_key_ref_and_plain_log_are_masked(self):
        value = 'opaque-sensitive-value'
        values, unresolved = manifest_redaction_values(self.manifest(
            {'stringData': {'odd-name': value}}, self.reference()), 'request-ns')
        self.assertFalse(unresolved)
        self.assertNotIn(value, redact('application printed ' + value, values))
        encoded = base64.b64encode(value.encode()).decode()
        self.assertNotIn(encoded, redact('serialized Secret ' + encoded, values))

    def test_data_base64_and_decoded_forms_are_masked(self):
        value = 'opaque-encoded-value'
        encoded = base64.b64encode(value.encode()).decode()
        values, unresolved = manifest_redaction_values(self.manifest(
            {'data': {'odd-name': encoded}}, self.reference()), 'request-ns')
        self.assertFalse(unresolved)
        for form in (value, encoded):
            self.assertNotIn(form, redact('logged ' + form, values))

    def test_string_data_overrides_data_same_key(self):
        encoded = base64.b64encode(b'old-value').decode()
        values, unresolved = manifest_redaction_values(self.manifest(
            {'data': {'odd-name': encoded}, 'stringData': {'odd-name': 'new-value'}},
            self.reference()), 'request-ns')
        self.assertFalse(unresolved)
        self.assertIn('new-value', values)

    def test_env_from_and_init_container_literals(self):
        rendered = self.manifest({'stringData': {'odd-name': 'from-env-secret'}},
            [{'name': 'CLIENT_AUTH', 'value': 'init-value'}],
            [{'secretRef': {'name': 'credentials'}}], init=True)
        values, unresolved = manifest_redaction_values(rendered, 'request-ns')
        self.assertFalse(unresolved)
        self.assertIn('from-env-secret', values)
        self.assertIn('init-value', values)

    def test_unknown_or_cross_namespace_secret_is_unresolved(self):
        for rendered in (self.manifest(env=self.reference()), self.manifest(
                {'stringData': {'odd-name': 'wrong-namespace-value'}}, self.reference(), namespace='other-ns')):
            values, unresolved = manifest_redaction_values(rendered, 'request-ns')
            self.assertTrue(unresolved)
            self.assertNotIn('wrong-namespace-value', values)

    def test_invalid_base64_or_unknown_key_is_unresolved(self):
        for secret in ({'data': {'odd-name': 'not-base64!'}}, {'stringData': {'different': 'value'}}):
            _, unresolved = manifest_redaction_values(self.manifest(secret, self.reference()), 'request-ns')
            self.assertTrue(unresolved)

    def test_short_url_encoded_and_json_escaped_values(self):
        value = 'private/@"value'
        values, _ = manifest_redaction_values(self.manifest({'stringData': {'odd-name': value}}), 'request-ns')
        for form in (value, quote(value, safe=''), json.dumps(value)[1:-1]):
            self.assertNotIn(form, redact('printed ' + form, values))
        self.assertEqual(redact('abc', ('abc',)), '[REDACTED]')

    def test_secret_volume_reference_is_checked(self):
        rendered = yaml.safe_dump({'kind': 'Pod', 'spec': {'volumes': [
            {'name': 'mount', 'secret': {'secretName': 'external-secret'}}]}})
        _, unresolved = manifest_redaction_values(rendered, 'request-ns')
        self.assertTrue(unresolved)

    def test_real_deployment_template_collects_generated_database_password(self):
        root = Path(__file__).resolve().parents[1]
        with patch('deploy_repository.secrets.token_hex', side_effect=['opaque-db-secret', 'opaque-bootstrap-secret']):
            rendered = render_template(root / 'k8s/quickbyte-demo.yaml', 'request-ns', 'test:latest', {'apiReplicas': 1})
        values, unresolved = manifest_redaction_values(rendered, 'request-ns')
        self.assertFalse(unresolved)
        self.assertIn('opaque-db-secret', values)
        self.assertIn('opaque-bootstrap-secret', values)
        self.assertNotIn('opaque-db-secret', redact('application printed opaque-db-secret', values))


if __name__ == '__main__':
    unittest.main()
