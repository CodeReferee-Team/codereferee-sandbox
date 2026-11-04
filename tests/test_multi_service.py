import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from execution_plan import ConfigurationRequired, resolve_plan
from multi_service import render_services
import deploy_repository as deploy
from in_cluster_probe import InClusterProbe
from artifact_cleanup import cleanup_request_image
from app.main import ChaosTarget, probe_environment


class MultiServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('gateway', 'api'):
            (self.root / name).mkdir()
            (self.root / name / 'Dockerfile').write_text('FROM python:3.12-slim\nEXPOSE 8000\n')
        self.data = {'version': 2, 'entryService': 'gateway', 'targetService': 'api', 'probePath': '/orders',
            'services': {'gateway': {'buildContext': 'gateway', 'dockerfile': 'gateway/Dockerfile',
                'port': 8000, 'healthPath': '/health', 'dependsOn': ['api'],
                'env': {'API_URL': '{{service.api.url}}'}},
                'api': {'buildContext': 'api', 'dockerfile': 'api/Dockerfile', 'port': 8001,
                    'healthPath': '/health', 'env': {'DATABASE_PASSWORD': '{{dependency.db.password}}',
                                                   'DATABASE_HOST': '{{dependency.db.host}}'}}},
            'dependencies': {'db': {'type': 'postgres'}}}
        (self.root / '.codereferee').mkdir()

    def resolve(self):
        (self.root / '.codereferee/validation.yaml').write_text(yaml.safe_dump(self.data))
        return resolve_plan(self.root)

    def test_shared_dependency_and_service_dns_with_distinct_fault_and_probe(self):
        plan = self.resolve()
        self.assertEqual(plan['serviceOrder'], ['api', 'gateway'])
        rendered, target, targets = render_services(plan, 'codereferee-test', {'api': 'api-image', 'gateway': 'web-image'})
        objects = list(yaml.safe_load_all(rendered))
        deployments = {o['metadata']['name']: o for o in objects if o['kind'] == 'Deployment'}
        self.assertEqual(set(deployments), {'db', 'cr-svc-api', 'cr-svc-gateway'})
        def env(name):
            return {v['name']: v['value'] for v in deployments[name]['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env('cr-svc-gateway')['API_URL'], 'http://cr-svc-api:8001')
        self.assertEqual(env('db')['POSTGRES_PASSWORD'], env('cr-svc-api')['DATABASE_PASSWORD'])
        self.assertEqual(target['deployment'], 'cr-svc-api')
        self.assertEqual(target['probeService'], 'cr-svc-gateway')
        self.assertEqual(target['probePath'], '/orders')
        self.assertEqual(set(targets), {'gateway', 'api'})
        environment = probe_environment(ChaosTarget.model_validate(target))
        with patch.dict(os.environ, environment):
            observer = InClusterProbe('codereferee-test', target['service'], target['servicePort'], path=target['probePath'])
        self.assertEqual(observer.url, 'http://cr-svc-gateway.codereferee-test.svc.cluster.local:8000/orders')

    def test_unknown_target_cycle_and_escaping_build_context_are_rejected(self):
        for key, value in (('targetService', 'missing'), ('entryService', 'missing')):
            with self.subTest(key=key):
                old = self.data[key]; self.data[key] = value
                with self.assertRaises(ConfigurationRequired): self.resolve()
                self.data[key] = old
        self.data['services']['api']['dependsOn'] = ['gateway']
        with self.assertRaisesRegex(ConfigurationRequired, 'cycle'): self.resolve()
        self.data['services']['api'].pop('dependsOn')
        self.data['services']['api']['buildContext'] = '../outside'
        with self.assertRaises(ConfigurationRequired): self.resolve()

    def test_missing_build_configuration_and_unknown_service_reference_are_rejected(self):
        self.data['services']['api']['env']['OTHER'] = '{{service.missing.url}}'
        with self.assertRaises(ConfigurationRequired): self.resolve()
        self.data['services']['api']['env'].pop('OTHER')
        self.data['services']['api']['dockerfile'] = 'missing.Dockerfile'
        with self.assertRaises(ConfigurationRequired): self.resolve()

    def test_dependency_cannot_overwrite_service_workload(self):
        self.data['dependencies']['cr-svc-api'] = {'type': 'redis'}
        with self.assertRaisesRegex(ConfigurationRequired, 'collides'): self.resolve()

    def test_invalid_dns_name_is_rejected_before_building(self):
        self.data['services']['bad-'] = self.data['services'].pop('api')
        self.data['services']['gateway']['dependsOn'] = ['bad-']
        with self.assertRaisesRegex(ConfigurationRequired, 'DNS'): self.resolve()

    def test_repository_profile_cannot_silently_replace_a_version_two_group(self):
        self.data['deploymentProfile'] = 'quickbyte-demo'
        with self.assertRaisesRegex(ConfigurationRequired, 'Unsupported version 2'): self.resolve()

    def test_resource_limit_origin_is_reported_per_resource_not_guessed_from_oom(self):
        self.data['services']['api']['resources'] = {'memory': '256Mi'}
        _, target, targets = render_services(self.resolve(), 'codereferee-test', {'api': 'api', 'gateway': 'web'})
        self.assertEqual(target['resourceConfiguration'], {'requestedLimits': {'cpu': '1000m', 'memory': '256Mi'},
            'sources': {'cpu': 'sandbox_default', 'memory': 'repository_configuration'}})
        self.assertEqual(targets['gateway']['resourceConfiguration']['sources']['memory'], 'sandbox_default')

    def deploy(self, failing_service=None, failing_load=False, namespace_removed=True):
        plan = self.resolve()
        args = argparse.Namespace(request_id='multi-test', repository_url='https://github.com/example/app',
            branch=None, commit_sha=None, profile=None, patch_file=None, rollout_timeout_seconds=1)
        applied, waited = [], []
        def clone(url, branch, destination):
            import shutil
            shutil.copytree(self.root, destination, ignore=shutil.ignore_patterns('.runtime'))
        def wait(namespace, name, timeout):
            waited.append(name)
            if name == failing_service: raise RuntimeError('rollout failed')
        def verify(*args):
            return {'outcome': 'passed', 'steps': [], 'test_execution': 'not_declared',
                    'verification_declared': False}
        with patch.object(deploy, 'RUNTIME_ROOT', self.root / '.runtime'), \
             patch.object(deploy, 'parse_args', return_value=args), patch.object(deploy, 'clone', side_effect=clone), \
             patch.object(deploy, 'run', return_value=subprocess.CompletedProcess([], 0, 'a' * 40, '')), \
             patch.object(deploy, 'verify_repository', side_effect=verify), \
             patch.object(deploy, 'load_image_into_cluster', side_effect=[None, RuntimeError('partial image load')] if failing_load else None), \
             patch.object(deploy, 'apply', side_effect=lambda text: applied.extend(yaml.safe_load_all(text))), \
             patch.object(deploy, 'wait_rollout', side_effect=wait), \
             patch.object(deploy, 'collect_pod_diagnostics', return_value={'pods': [{'name': 'bad-pod'}]}), \
             patch.object(deploy, 'delete_namespace', return_value=namespace_removed), \
             patch.object(deploy, 'cleanup_request_image', return_value={'errors': []}) as cleanup:
            out = io.StringIO()
            with redirect_stdout(out): code = deploy.main()
        return code, json.loads(out.getvalue()), waited, cleanup.call_args_list

    def test_deploy_checks_each_service_and_waits_for_dependencies_before_apps(self):
        code, payload, waited, _ = self.deploy()
        self.assertEqual(code, 0)
        self.assertEqual(waited, ['db', 'cr-svc-api', 'cr-svc-gateway'])
        self.assertEqual(len(payload['images']), 2)
        self.assertEqual(set(payload['sandboxReport']['services']), {'api', 'gateway'})
        self.assertEqual(len(payload['sandboxReport']['service_timings']), 9)
        self.assertFalse(payload['sandboxReport']['verification_declared'])

    def add_four_service_chain(self):
        for name in ('catalog', 'storage'):
            (self.root / name).mkdir()
            (self.root / name / 'Dockerfile').write_text('FROM python:3.12-slim\nEXPOSE 8000\n')
            self.data['services'][name] = {'buildContext': name, 'dockerfile': name + '/Dockerfile',
                                          'port': 8000, 'healthPath': '/health'}
        self.data['services']['api']['dependsOn'] = ['catalog']
        self.data['services']['catalog']['dependsOn'] = ['storage']

    def test_four_services_follow_dependency_order_and_return_all_images(self):
        self.add_four_service_chain()
        code, payload, waited, _ = self.deploy()
        self.assertEqual(code, 0)
        self.assertEqual(waited, ['db', 'cr-svc-storage', 'cr-svc-catalog', 'cr-svc-api', 'cr-svc-gateway'])
        self.assertEqual(len(payload['images']), 4)
        self.assertEqual(set(payload['services']), {'storage', 'catalog', 'api', 'gateway'})

    def test_four_service_rollout_failure_cleans_every_built_image(self):
        self.add_four_service_chain()
        code, payload, waited, cleanup = self.deploy('cr-svc-api')
        self.assertEqual(code, 1)
        self.assertEqual(waited, ['db', 'cr-svc-storage', 'cr-svc-catalog', 'cr-svc-api'])
        self.assertEqual(payload['result']['sandboxReport']['failed_service'], 'api')
        self.assertEqual(len(cleanup), 4)

    def test_more_than_four_services_requires_configuration(self):
        self.add_four_service_chain()
        self.data['services']['extra'] = dict(self.data['services']['storage'])
        with self.assertRaisesRegex(ConfigurationRequired, '2..4'):
            self.resolve()

    def test_three_services_render_one_workload_and_image_per_service(self):
        self.add_four_service_chain()
        del self.data['services']['storage']
        self.data['services']['catalog'].pop('dependsOn')
        code, payload, waited, _ = self.deploy()
        self.assertEqual(code, 0)
        self.assertEqual(waited, ['db', 'cr-svc-catalog', 'cr-svc-api', 'cr-svc-gateway'])
        self.assertEqual(len(payload['images']), 3)

    def test_second_service_failure_keeps_identity_and_diagnostics_and_cleans_both_images(self):
        code, payload, _, cleanup = self.deploy('cr-svc-gateway')
        self.assertEqual(code, 1)
        report = payload['result']['sandboxReport']
        self.assertEqual(report['failed_service'], 'gateway')
        self.assertEqual(report['pod_diagnostics']['service'], 'gateway')
        self.assertEqual(report['pod_diagnostics']['pods'][0]['name'], 'bad-pod')
        self.assertEqual(len(cleanup), 2)
        self.assertTrue(all(c.kwargs['remove_from_kind'] for c in cleanup))

    def test_partial_image_load_before_namespace_is_also_cleaned(self):
        _, _, waited, cleanup = self.deploy(failing_load=True)
        self.assertEqual(waited, [])
        self.assertEqual(len(cleanup), 2)
        self.assertTrue(all(c.kwargs['remove_from_kind'] for c in cleanup))

    def test_running_images_are_kept_on_nodes_when_namespace_cleanup_fails(self):
        _, payload, _, cleanup = self.deploy('cr-svc-gateway', namespace_removed=False)
        self.assertFalse(payload['result']['source']['artifact_cleanup']['namespace_removed'])
        self.assertEqual(len(cleanup), 2)
        self.assertFalse(any(c.kwargs['remove_from_kind'] for c in cleanup))

    def test_service_metrics_are_separate_from_the_fault_target_series(self):
        from scripts.metrics_lifecycle import RequestMetrics
        _, target, _ = render_services(self.resolve(), 'codereferee-test', {'api': 'api', 'gateway': 'web'})
        with patch.dict(os.environ, {}, clear=True):
            session = RequestMetrics('test', target)
        session.service_bindings = {
            'api': {('api-uid', 'api'): {'pod': 'api-pod', 'uid': 'api-uid', 'container': 'api'}},
            'gateway': {('gateway-uid', 'api'): {'pod': 'gateway-pod', 'uid': 'gateway-uid', 'container': 'api'}}}
        session.bindings = {('api-uid', 'api'): {'pod': 'api-pod'}}
        with patch.object(session, 'query', return_value=[{'values': [[1, '0.1']]}]) as query:
            result = session.collect_service_metrics(10)
        self.assertEqual(set(result), {'api', 'gateway'})
        self.assertEqual(result['gateway']['pod_bindings'][0]['uid'], 'gateway-uid')
        self.assertTrue(all(r['status'] == 'collected' for r in result.values()))
        self.assertEqual(sum('gateway' in c.args[0] for c in query.call_args_list), 2)
        self.assertNotIn('gateway', session.selector('container_cpu_usage_seconds_total'))

    def test_multi_service_agent_collects_request_namespace_not_only_fault_pods(self):
        from scripts.metrics_lifecycle import RequestMetrics
        _, target, _ = render_services(self.resolve(), 'codereferee-test', {'api': 'api', 'gateway': 'web'})
        with patch.dict(os.environ, {'CODEREFEREE_METRICS_REMOTE_WRITE_URL': 'http://receiver/write',
                                   'CODEREFEREE_METRICS_QUERY_URL': 'http://receiver'}, clear=True):
            session = RequestMetrics('test', target)
        with patch('scripts.metrics_lifecycle.wiring.apply', side_effect=RuntimeError('stop before cluster apply')) as apply:
            session.start()
        rendered = apply.call_args.args[0]
        self.assertIn('regex: "codereferee-test"', rendered)
        self.assertIn('regex: ".+"', rendered)
        self.assertNotIn('regex: "cr-svc-api-', rendered)

    def test_cleanup_accepts_only_images_owned_by_the_request(self):
        image = 'codereferee/multi-test/svc-api:' + 'a' * 12
        with patch('artifact_cleanup.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            self.assertTrue(cleanup_request_image(image, 'codereferee-multi-test', remove_from_kind=False)['host_removed'])
            with self.assertRaises(ValueError):
                cleanup_request_image(image, 'codereferee-other', remove_from_kind=False)

    def test_build_command_output_uses_utf8_on_windows_too(self):
        with patch.object(deploy, 'RUNTIME_ROOT', self.root):
            result = deploy.run([sys.executable, '-c', "print('빌드 완료 \\u2713')"])
        self.assertIn('빌드 완료', result.stdout)
        self.assertIn('\u2713', result.stdout)

    def test_cleanup_of_shared_image_tag_already_absent_on_kind_is_success(self):
        image = 'codereferee/multi-test/svc-api:' + 'a' * 12
        with patch.dict(os.environ, {'CODEREFEREE_CLUSTER_PROVIDER': 'kind'}), \
             patch('artifact_cleanup.subprocess.run', side_effect=[
                 subprocess.CompletedProcess([], 0, 'kind-node\n', ''),
                 subprocess.CompletedProcess([], 1, '', 'level=fatal msg="no such image ' + image + '"'),
                 subprocess.CompletedProcess([], 0, '', '')]):
            report = cleanup_request_image(image, 'codereferee-multi-test')
        self.assertEqual(report['errors'], [])
        self.assertTrue(report['kind_nodes'][0]['removed'])
        self.assertTrue(report['kind_nodes'][0]['already_absent'])

    def test_http_request_cleans_all_service_images_even_if_one_cleanup_fails(self):
        import app.main as api
        _, target, targets = render_services(self.resolve(), 'codereferee-test', {'api': 'api', 'gateway': 'web'})
        images = ['codereferee/test/svc-api:' + 'a' * 12, 'codereferee/test/svc-gateway:' + 'a' * 12]
        payload = {'target': target, 'services': targets, 'images': images, 'image': images[0],
                   'repository': {'commitSha': 'a' * 40}}
        observed = subprocess.CompletedProcess([], 0, json.dumps({'observationStatus': 'observed', 'exitCode': 0}), '')
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(api, 'deploy_repository', return_value=payload), \
             patch.object(api, 'run_bounded', return_value=observed), \
             patch.object(api, 'cleanup_namespace', return_value=True), \
             patch('scripts.artifact_cleanup.cleanup_request_image', side_effect=[OSError('cleanup test'), {}]) as cleanup:
            result = api.validate_repository(api.RepositoryValidationRequest(repositoryUrl='https://github.com/example/app',
                requestId='test', chaosMode='litmus_container_kill'))
        self.assertEqual([call.args[0] for call in cleanup.call_args_list], images)
        self.assertEqual(result['source']['probe_endpoint']['service'], 'cr-svc-gateway')
        self.assertEqual(len(result['source']['artifact_cleanup']['images']), 2)
        self.assertEqual(result['source']['artifact_cleanup']['errors'], ['cleanup test'])

    def test_runtime_failure_includes_peer_service_status_without_free_text(self):
        import app.main as api
        _, target, _ = render_services(self.resolve(), 'codereferee-test', {'api': 'api', 'gateway': 'web'})
        observed = subprocess.CompletedProcess([], 1, json.dumps({'observationStatus': 'observed', 'exitCode': 1}), '')
        def diagnostic(_namespace, deployment, _values, *, include_text):
            if include_text: return {'logs_tail': 'secret-sentinel'}
            return {'pods': [{'name': deployment, 'reason': 'OOMKilled' if deployment.endswith('gateway') else 'Running'}]}
        with patch.dict(os.environ, {}, clear=True), patch.object(api, 'run_bounded', return_value=observed), \
             patch('scripts.pod_diagnostics.collect_pod_diagnostics', side_effect=diagnostic):
            result = api.validate_repository(api.RepositoryValidationRequest(repositoryUrl='https://github.com/example/app',
                requestId='test', chaosMode='litmus_container_kill', chaosTarget=api.ChaosTarget.model_validate(target)))
        self.assertEqual(result['source']['service_diagnostics']['gateway']['pods'][0]['reason'], 'OOMKilled')
        self.assertEqual(result['source']['workload_resources']['gateway']['sources']['memory'], 'sandbox_default')
        self.assertNotIn('secret-sentinel', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
