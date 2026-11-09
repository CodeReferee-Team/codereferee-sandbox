import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from execution_plan import ConfigurationRequired, resolve_plan
from browser_probe import BrowserProbeRunner, validate_browser_probe
from http_proxy import validate_proxy_routes, proxy_config
from multi_service import render_services
from app.main import (ChaosTarget, RepositoryValidationRequest, apply_browser_report,
                      browser_failure_result, strip_private_probe_specs, validate_repository)


SPEC = {'steps': [{'action': 'goto', 'path': '/'},
                  {'action': 'fill', 'selector': '#name', 'value': 'item-${runId}'},
                  {'action': 'click', 'selector': '#save', 'expectResponse': {
                      'path': '/api/items', 'status': 201, 'expect': {'saved': True}}},
                  {'action': 'assertText', 'selector': '#items', 'text': 'item-${runId}'}]}
TARGET = {'namespace': 'codereferee-browser-test', 'deployment': 'api', 'service': 'api',
          'servicePort': 8000, 'labelSelector': 'app=api', 'probeService': 'web',
          'probeServicePort': 8080, 'browserProbe': SPEC}


class BrowserContractTests(unittest.TestCase):
    def test_declarative_journey_and_bounded_input(self):
        self.assertEqual(validate_browser_probe(SPEC), SPEC)
        for spec in ({'steps': []}, {'steps': [{'action': 'eval', 'value': 'alert(1)'}]},
                     {'steps': [{'action': []}, {'action': 'assertVisible', 'selector': '#x'}]},
                     dict(SPEC, timeoutSeconds=True), dict(SPEC, timeoutSeconds=600)):
            with self.subTest(spec=spec), self.assertRaises(ConfigurationRequired):
                validate_browser_probe(spec)

    def test_external_navigation_and_assertionless_journey_are_rejected(self):
        for path in ('https://example.com', '//example.com', '/\\example', '/\nheader'):
            with self.subTest(path=path), self.assertRaises(ConfigurationRequired):
                validate_browser_probe({'steps': [{'action': 'goto', 'path': path}, SPEC['steps'][-1]]})
        with self.assertRaises(ConfigurationRequired):
            validate_browser_probe({'steps': SPEC['steps'][:2]})

    def test_proxy_targets_are_declared_services_and_not_urls(self):
        self.assertEqual(validate_proxy_routes({'/api/': {'service': 'api', 'stripPrefix': True}},
                                              {'api': {}, 'web': {}}, 'web')[0]['service'], 'api')
        for routes in ({'/': {'service': 'api'}}, {'/api/': {'service': 'http://metadata'}},
                       {'/api/': {'service': 'web'}}, {'/api/': {'service': 'api', 'stripPrefix': 'true'}},
                       {'/api/;': {'service': 'api'}}):
            with self.subTest(routes=routes), self.assertRaises(ConfigurationRequired):
                validate_proxy_routes(routes, {'api': {}, 'web': {}}, 'web')

    def test_proxy_configuration_and_generated_service_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'web').mkdir(); (root/'api').mkdir(); (root/'.codereferee').mkdir()
            (root/'web/index.html').write_text('<div>example</div>')
            (root/'web/package.json').write_text(json.dumps({'devDependencies': {'vite': '8.0.8'},
                                                            'scripts': {'build': 'vite build'}}))
            (root/'api/app.py').write_text('from fastapi import FastAPI\napp=FastAPI()')
            config = {'version': 2, 'services': {'web': {'buildContext': 'web', 'proxyRoutes': {
                '/api/': {'service': 'api', 'stripPrefix': True}}}, 'api': {'buildContext': 'api'}},
                'entryService': 'web', 'targetService': 'api', 'browserProbe': SPEC}
            (root/'.codereferee/validation.yaml').write_text(yaml.safe_dump(config))
            plan = resolve_plan(root)
            self.assertEqual(plan['serviceOrder'], ['api', 'web'])
            manifest, target, _ = render_services(plan, 'codereferee-browser-test', {'api': 'api', 'web': 'web'})
            documents = list(yaml.safe_load_all(manifest))
            maps = [d for d in documents if d['kind'] == 'ConfigMap']
            self.assertIn('proxy_pass http://cr-svc-api:8000/;', maps[0]['data']['routes.conf'])
            self.assertIn('proxy_next_upstream off;', maps[0]['data']['routes.conf'])
            self.assertEqual(target['browserProbe'], SPEC)
            self.assertEqual(target['probeService'], 'cr-svc-web')
            config['services']['api']['dependsOn'] = ['web']
            (root/'.codereferee/validation.yaml').write_text(yaml.safe_dump(config))
            with self.assertRaisesRegex(ConfigurationRequired, 'cycle'):
                resolve_plan(root)

    def test_preserving_and_stripping_prefix_are_distinct(self):
        config = proxy_config([{'prefix': '/api/', 'service': 'api', 'stripPrefix': False}], {'api': ('backend', 8000)})
        self.assertIn('proxy_pass http://backend:8000;', config)


class BrowserRunnerTests(unittest.TestCase):
    def run_probe(self, payload, code=0):
        commands = []
        def run(command, **kwargs):
            commands.append((command, kwargs))
            if 'get' in command:
                body = {'status': {'phase': 'Succeeded' if code == 0 else 'Failed',
                                  'containerStatuses': [{'state': {'terminated': {'exitCode': code}}}]}}
                return subprocess.CompletedProcess(command, 0, json.dumps(body), '')
            if 'logs' in command:
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), '')
            return subprocess.CompletedProcess(command, 0, '', '')
        with patch('browser_probe.subprocess.run', side_effect=run):
            report = BrowserProbeRunner(ChaosTarget.model_validate(TARGET)).run(SPEC)
        return report, commands

    def test_real_completion_evidence_and_nonroot_pod(self):
        payload = {'schemaVersion': 'browser-probe.v1', 'observationStatus': 'observed',
                   'exitCode': 0, 'completedSteps': 4, 'failedStep': None, 'rawSecret': 'must-not-leak'}
        report, commands = self.run_probe(payload)
        self.assertEqual(report['exitCode'], 0)
        self.assertNotIn('rawSecret', report)
        pod = json.loads(commands[0][1]['input'])
        self.assertFalse(pod['spec']['automountServiceAccountToken'])
        self.assertTrue(pod['spec']['securityContext']['runAsNonRoot'])
        self.assertIn('chromium_sandbox=True', pod['spec']['containers'][0]['command'][-1])
        self.assertTrue(any('delete' in c for c, _ in commands))

    def test_missing_steps_or_crashed_runner_never_pass(self):
        payload = {'schemaVersion': 'browser-probe.v1', 'observationStatus': 'observed',
                   'exitCode': 0, 'completedSteps': 0}
        for code in (0, 137):
            report, _ = self.run_probe(payload, code)
            self.assertEqual(report['observationStatus'], 'infrastructure_error')
            self.assertIsNone(report['exitCode'])

    def test_runtime_failure_is_distinct_from_functional_failure(self):
        report, _ = self.run_probe({'errorCode': 'browser_launch_failed'}, 2)
        self.assertEqual(report['errorCode'], 'browser_launch_failed')
        payload = {'schemaVersion': 'browser-probe.v1', 'observationStatus': 'observed',
                   'exitCode': 1, 'completedSteps': 2, 'failedStep': 2, 'errorCode': 'browser_assertion_failed'}
        report, _ = self.run_probe(payload, 1)
        self.assertEqual(report['exitCode'], 1)


class BrowserIntegrationTests(unittest.TestCase):
    def test_http_success_does_not_hide_browser_failure(self):
        result = {'observationStatus': 'observed', 'exitCode': 0, 'metrics': {'availability': 1},
                  'chaos_observation': {'recovered': True, 'recovery_seconds': 2}}
        request = RepositoryValidationRequest(repositoryUrl='https://github.com/example/app')
        apply_browser_report(result, {'observationStatus': 'observed', 'exitCode': 1}, request, 'browser:after')
        self.assertEqual(result['exitCode'], 1)
        self.assertEqual(result['metrics']['availability'], 1)
        self.assertEqual(result['source']['http_execution']['exitCode'], 0)
        self.assertFalse(result['chaos_observation']['recovered'])
        self.assertIsNone(result['chaos_observation']['recovery_seconds'])
        self.assertEqual(result['source']['http_execution']['chaos_observation']['recovery_seconds'], 2)

    def test_private_values_are_removed(self):
        result = {'deployment': {'target': {'browserProbe': SPEC, 'probeSpec': {'password': 'secret'}}}}
        strip_private_probe_specs(result)
        self.assertEqual(result['deployment']['target'], {})

    def test_existing_http_failure_and_infrastructure_error_are_preserved(self):
        request = RepositoryValidationRequest(repositoryUrl='https://github.com/example/app')
        for status, code in (('infrastructure_error', None), ('observed', 1)):
            result = {'observationStatus': status, 'exitCode': code, 'stderr': 'original evidence'}
            apply_browser_report(result, {'observationStatus': 'observed', 'exitCode': 1}, request, 'browser:after')
            self.assertEqual((result['observationStatus'], result['exitCode']), (status, code))
            self.assertEqual(result['stderr'], 'original evidence')

    def test_failed_browser_baseline_skips_fault(self):
        request = RepositoryValidationRequest(repositoryUrl='https://github.com/example/app', requestId='browser-test',
            chaosMode='litmus_container_kill', chaosTarget=TARGET)
        failed = {'observationStatus': 'observed', 'exitCode': 1, 'completedSteps': 1,
                  'errorCode': 'browser_assertion_failed'}
        with patch('app.main.BrowserProbeRunner') as runner, patch('app.main.run_bounded') as execute:
            runner.return_value.run.return_value = failed
            runner.return_value.image = 'test'
            result = validate_repository(request)
        execute.assert_not_called()
        self.assertEqual(result['failureKind'], 'browser_workflow')
        self.assertFalse(result['source']['chaos_injected'])
        self.assertTrue(result['browserCheckAttempted'])


class BrowserProvisioningTests(unittest.TestCase):
    def test_kind_runtime_and_profile_reach_every_node(self):
        from build_browser_runtime import main
        commands = []
        def invoke(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, 'demo-control-plane\ndemo-worker\n', '')
        with patch('sys.argv', ['build_browser_runtime.py', '--kind-cluster', 'demo']), \
                patch('build_browser_runtime.shutil.disk_usage', return_value=SimpleNamespace(free=6*1024**3)), \
                patch('build_browser_runtime.subprocess.run', side_effect=invoke), \
                patch.dict(os.environ, {'CODEREFEREE_KIND_COMMAND': 'kind-local'}):
            main()
        self.assertEqual(commands[1][:3], ['kind-local', 'load', 'docker-image'])
        copies = [command for command in commands if command[:2] == ['docker', 'cp']]
        self.assertEqual(len(copies), 2)
        self.assertTrue(all(command[-1].endswith(':/var/lib/kubelet/seccomp/codereferee-browser.json') for command in copies))

    def test_failed_build_stops_provisioning(self):
        from build_browser_runtime import main
        with patch('sys.argv', ['build_browser_runtime.py', '--kind-cluster', 'demo']), \
                patch('build_browser_runtime.shutil.disk_usage', return_value=SimpleNamespace(free=6*1024**3)), \
                patch('build_browser_runtime.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'docker')) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                main()
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
