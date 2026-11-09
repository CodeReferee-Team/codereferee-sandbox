import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from execution_plan import ConfigurationRequired, node_commands, resolve_plan
from repository_checks import check_plan
from vite_static import EXPORT_OUTPUT, build_options
import deploy_repository


class ViteStaticTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.package = {'scripts': {'dev': 'vite', 'build': 'tsc -b && vite build', 'test': 'node --test'},
                        'devDependencies': {'vite': '^8.0.0'}}
        (self.root / 'index.html').write_text('<div id="root"></div>', encoding='utf-8')
        self.save()

    def save(self):
        (self.root / 'package.json').write_text(json.dumps(self.package), encoding='utf-8')

    def test_spa_uses_production_static_server_and_consistent_check_runtime(self):
        plan = resolve_plan(self.root)
        recipe = plan['generatedDockerfile']
        self.assertEqual((plan['port'], plan['healthPathDeclared']), (8080, True))
        self.assertIn('FROM node:22-bookworm-slim AS builder', recipe)
        self.assertIn('RUN ["npm", "run", "build"]', recipe)
        self.assertIn('nginx-unprivileged:stable-alpine@sha256:', recipe)
        self.assertIn('USER 101:101', recipe)
        self.assertNotIn('"run","dev"', recipe)
        self.assertEqual(check_plan(self.root)['image'], 'node:22-bookworm')

    def test_explicit_dockerfile_still_wins(self):
        (self.root / 'Dockerfile').write_text('FROM nginx\nEXPOSE 80\n', encoding='utf-8')
        self.assertEqual(resolve_plan(self.root)['source'], 'dockerfile')

    def test_declared_node_server_is_not_replaced_by_static_frontend(self):
        self.package['scripts']['start'] = 'node server.js'
        self.save()
        recipe = resolve_plan(self.root)['generatedDockerfile']
        self.assertIn('CMD ["npm","run","start"]', recipe)
        self.assertNotIn('nginx-unprivileged', recipe)
        del self.package['scripts']['start']
        self.package['scripts']['dev'] = 'node server.js'
        self.save()
        self.assertNotIn('nginx-unprivileged', resolve_plan(self.root)['generatedDockerfile'])

    def test_custom_build_flags_and_ambiguous_server_builds(self):
        self.assertEqual(build_options('vite build --outDir public-build --base=/web/ --mode staging'),
                         {'build': {'outDir': 'public-build'}, 'base': '/web/', 'mode': 'staging'})
        for script in ('vite build --ssr', 'vite build --outDir', 'next build',
                       'vite build; echo hi', 'vite build --base=$PUBLIC_PATH'):
            with self.subTest(script=script), self.assertRaises(ValueError):
                build_options(script)

    def test_vite_without_build_is_configuration_required(self):
        del self.package['scripts']['build']
        self.save()
        with self.assertRaises(ConfigurationRequired):
            resolve_plan(self.root)

    def test_pinned_pnpm_is_shared_by_install_build_and_test(self):
        self.package['packageManager'] = 'pnpm@10.33.0'
        self.save()
        (self.root / 'pnpm-lock.yaml').write_text('lockfileVersion: 9.0\n', encoding='utf-8')
        plan = check_plan(self.root)
        self.assertEqual(plan['install'][0][-2:], ['install', '--frozen-lockfile'])
        self.assertEqual(plan['build'][-3:], ['pnpm', 'run', 'build'])
        self.assertEqual(plan['test'][-3:], ['pnpm', 'run', 'test'])
        self.assertIn('pnpm@10.33.0', resolve_plan(self.root)['generatedDockerfile'])
        (self.root / 'package-lock.json').write_text('{}', encoding='utf-8')
        with self.assertRaises(ConfigurationRequired):
            node_commands(self.root, self.package)

    def test_unpinned_manager_and_unknown_lock_are_not_installed_with_npm(self):
        (self.root / 'pnpm-lock.yaml').write_text('{}', encoding='utf-8')
        with self.assertRaises(ConfigurationRequired):
            check_plan(self.root)
        self.package['packageManager'] = 'pnpm@latest'
        with self.assertRaises(ConfigurationRequired):
            node_commands(self.root, self.package)

    def test_generated_service_defaults_survive_explicit_and_multi_service_declarations(self):
        (self.root / '.codereferee').mkdir()
        config = self.root / '.codereferee/validation.yaml'
        config.write_text('version: 1\nservice: {env: {PUBLIC_VALUE: example}}\n', encoding='utf-8')
        self.assertEqual(resolve_plan(self.root)['port'], 8080)
        config.write_text('version: 2\nservices:\n  web: {}\n  other: {}\nentryService: web\n', encoding='utf-8')
        self.assertEqual(resolve_plan(self.root)['services']['web']['port'], 8080)

    def test_output_export_guards_and_asset_404_are_in_builder(self):
        self.assertIn('await resolveConfig', EXPORT_OUTPUT)
        self.assertIn('config.build.ssr || config.build.lib', EXPORT_OUTPUT)
        self.assertIn('relative.startsWith', EXPORT_OUTPUT)
        self.assertIn('entry.isSymbolicLink()', EXPORT_OUTPUT)
        self.assertIn('try_files $uri =404', EXPORT_OUTPUT)
        self.assertIn('absolute_redirect off;', EXPORT_OUTPUT)

    def test_unsupported_static_build_requests_configuration_instead_of_app_failure(self):
        failure = subprocess.CompletedProcess([], 1, '',
            '#7 0.2 Error: CODEREFEREE_STATIC_CONFIGURATION_REQUIRED: SSR requires a server contract')
        with patch.object(deploy_repository.subprocess, 'run', return_value=failure):
            with self.assertRaises(ConfigurationRequired) as caught:
                deploy_repository.run(['docker', 'build', '.'])
        self.assertEqual(str(caught.exception),
                         'CODEREFEREE_STATIC_CONFIGURATION_REQUIRED: SSR requires a server contract')
        failure.stderr = 'Application build failed'
        with patch.object(deploy_repository.subprocess, 'run', return_value=failure):
            with self.assertRaises(RuntimeError):
                deploy_repository.run(['docker', 'build', '.'])
        failure.stderr = "RUN node -e \"throw new Error('CODEREFEREE_STATIC_CONFIGURATION_REQUIRED:')\"\nENOSPC"
        with patch.object(deploy_repository.subprocess, 'run', return_value=failure):
            with self.assertRaises(RuntimeError):
                deploy_repository.run(['docker', 'build', '.'])


if __name__ == '__main__':
    unittest.main()
