import json
from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from execution_plan import ConfigurationRequired, inside, normalize_resources, render_plan, resolve_plan


class ExecutionPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def test_explicit_and_checked_in_configuration_override_inference(self):
        self.write('.codereferee/validation.yaml', 'deploymentProfile: quickbyte-demo\n')
        self.assertEqual(resolve_plan(self.root)['profile'], 'quickbyte-demo')
        self.assertEqual(resolve_plan(self.root, 'custom')['profile'], 'custom')

    def test_dockerfile_single_exposed_port_is_inferred_without_profile(self):
        self.write('Dockerfile', 'FROM node:20\nEXPOSE 3000\n')
        plan = resolve_plan(self.root)
        self.assertEqual(plan['source'], 'dockerfile')
        self.assertEqual(plan['port'], 3000)
        manifest, target = render_plan(plan, 'codereferee-test', 'codereferee/test:123456789abc')
        self.assertEqual(target['deployment'], 'repository-api')
        self.assertIn('automountServiceAccountToken: false', manifest)

    def test_ambiguous_ports_and_library_require_configuration(self):
        with self.assertRaises(ConfigurationRequired):
            resolve_plan(self.root)
        self.write('Dockerfile', 'FROM nginx\nEXPOSE 80 443\n')
        with self.assertRaises(ConfigurationRequired):
            resolve_plan(self.root)

    def test_node_without_dockerfile_gets_generated_recipe(self):
        self.write('package.json', json.dumps({'scripts': {'start': 'node src/index.js'}}))
        plan = resolve_plan(self.root)
        self.assertEqual(plan['source'], 'stack_detection')
        self.assertIn('CMD ["npm","run","start"]', plan['generatedDockerfile'])

    def test_repository_paths_cannot_escape_clone(self):
        with self.assertRaises(ConfigurationRequired):
            inside(self.root, '../Dockerfile')
        with self.assertRaises(ConfigurationRequired):
            inside(self.root, '.git/config')

    def test_compose_redis_network_is_reproduced_without_host_mounts(self):
        self.write('Dockerfile', 'FROM python:3.12\nEXPOSE 5000\n')
        self.write('compose.yaml', 'services:\n  web:\n    build: .\n    ports: ["8000:5000"]\n    environment: {REDIS_HOST: redis}\n  redis:\n    image: redis:7-alpine\n')
        plan = resolve_plan(self.root)
        self.assertEqual(plan['source'], 'compose')
        self.assertEqual(plan['port'], 5000)
        manifest, _ = render_plan(plan, 'codereferee-test', 'test')
        self.assertIn('redis:7-alpine', manifest)
        self.assertNotIn('hostPath', manifest)

    def test_multi_service_compose_does_not_silently_choose_a_target(self):
        self.write('compose.yaml', 'services:\n  a: {build: .}\n  b: {build: .}\n')
        with self.assertRaises(ConfigurationRequired):
            resolve_plan(self.root)

    def test_yaml_postgres_password_is_generated_and_shared_with_app_only(self):
        self.write('Dockerfile', 'FROM python:3.12\n')
        self.write('.codereferee/validation.yaml', 'version: 1\nservice:\n  port: 8000\n  env:\n    DB_PASSWORD: "{{dependency.db.password}}"\ndependencies:\n  db: {type: postgres}\n')
        import yaml
        objects = list(yaml.safe_load_all(render_plan(resolve_plan(self.root), 'codereferee-test', 'test')[0]))
        passwords = []
        for obj in objects:
            if obj['kind'] == 'Deployment':
                for env in obj['spec']['template']['spec']['containers'][0].get('env', []):
                    if 'PASSWORD' in env['name']:
                        passwords.append(env['value'])
        self.assertEqual(len(passwords), 2)
        self.assertEqual(passwords[0], passwords[1])
        self.assertEqual(len(passwords[0]), 48)

    def test_host_environment_interpolation_is_not_loaded(self):
        self.write('Dockerfile', 'FROM node:20\n')
        self.write('.codereferee/validation.yaml', 'version: 1\nservice:\n  port: 3000\n  env: {TOKEN: "${SECRET}"}\n')
        with self.assertRaises(ConfigurationRequired):
            resolve_plan(self.root)

    def test_resource_requests_never_exceed_small_declared_limits(self):
        limits = normalize_resources({'cpu': '50m', 'memory': '32Mi'})
        self.assertEqual(limits['requests'], {'cpu': '50m', 'memory': '32Mi'})
        with self.assertRaises(ConfigurationRequired):
            normalize_resources({'memory': '8Gi'})
