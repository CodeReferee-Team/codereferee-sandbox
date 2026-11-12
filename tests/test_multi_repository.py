import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from execution_plan import ConfigurationRequired
from multi_repository import resolve_repository_group, validate_sources
from multi_service import render_services


class MultiRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.main = self.workspace / 'repository'
        self.main.mkdir()
        (self.main / 'Dockerfile').write_text('FROM scratch\nEXPOSE 8000\n')
        self.declaration = {'version': 3, 'repositories': {
            'backend': {'url': 'https://github.com/example/backend.git', 'branch': 'main'}},
            'services': {'web': {'port': 8000, 'dependsOn': ['api']},
                         'api': {'repository': 'backend', 'port': 8000}},
            'entryService': 'web', 'targetService': 'api'}
        self.primary = {'url': 'https://github.com/example/frontend.git', 'commitSha': 'a'*40}
        self.state = {}

    def clone(self, url, branch, destination):
        destination.mkdir()
        (destination / 'Dockerfile').write_text('FROM scratch\nEXPOSE 8000\n')

    def resolve(self, data=None, run=None):
        run = run or Mock(return_value=subprocess.CompletedProcess(['git'], 0, 'b'*40+'\n', ''))
        return resolve_repository_group(self.main, self.workspace, data or self.declaration,
                                        self.primary, self.clone, run, self.state)

    def test_sources_are_isolated_and_commits_are_exposed(self):
        plan = self.resolve()
        self.assertEqual(plan['source'], 'repository_group')
        self.assertEqual(plan['repositories']['backend']['commitSha'], 'b'*40)
        self.assertEqual(plan['services']['api']['repositoryName'], 'backend')
        self.assertEqual(plan['serviceOrder'], ['api', 'web'])
        self.assertEqual(self.state['repository_roots']['backend'], self.workspace/'sources/backend')
        self.assertEqual(self.state['service_repositories'], {'web': 'main', 'api': 'backend'})
        _, target, _ = render_services(plan, 'codereferee-test', {'web': 'web:1', 'api': 'api:1'})
        self.assertEqual(target['targetService'], 'api')

    def test_repository_url_validation_precedes_clone(self):
        for url in ('file:///tmp/repo', 'http://github.com/o/r', 'https://token@github.com/o/r',
                    'https://github.com/../r', 'https://github.com/o/r?token=x'):
            self.declaration['repositories']['backend']['url'] = url
            clone = Mock()
            with self.subTest(url=url), self.assertRaises(ConfigurationRequired):
                resolve_repository_group(self.main, self.workspace, self.declaration, self.primary,
                                         clone, Mock(), self.state)
            clone.assert_not_called()

    def test_invalid_source_references_and_unused_sources_are_rejected(self):
        for name in ('missing', [], 'main'):
            self.declaration['services']['api']['repository'] = name
            with self.subTest(name=name), self.assertRaises(ConfigurationRequired):
                validate_sources(self.declaration)

    def test_secondary_repository_paths_cannot_escape(self):
        self.declaration['services']['api']['buildContext'] = '../../repository'
        with self.assertRaises(ConfigurationRequired):
            self.resolve()

    def test_full_sha_is_checked_out_and_resolved_before_build(self):
        self.declaration['repositories']['backend']['commitSha'] = 'b'*40
        runner = Mock(return_value=subprocess.CompletedProcess(['git'], 0, 'b'*40+'\n', ''))
        self.resolve(run=runner)
        self.assertEqual(runner.call_args_list[0].args[0], ['git', 'fetch', '--depth', '1', 'origin', 'b'*40])
        self.assertEqual(runner.call_args_list[1].args[0], ['git', 'checkout', '--detach', 'b'*40])

    def test_preparation_retry_uses_previous_source_commit(self):
        snapshots = {'backend': {'url': 'https://github.com/example/backend.git', 'commitSha': 'c'*40}}
        runner = Mock(return_value=subprocess.CompletedProcess(['git'], 0, 'c'*40+'\n', ''))
        with patch.dict(os.environ, {'CODEREFEREE_REPOSITORY_SNAPSHOTS': json.dumps(snapshots)}):
            plan = self.resolve(run=runner)
        self.assertEqual(plan['repositories']['backend']['commitSha'], 'c'*40)
        self.assertIn('c'*40, runner.call_args_list[0].args[0])

    def test_four_additional_sources_five_services(self):
        for index in range(3):
            alias = 'peer'+str(index)
            self.declaration['repositories'][alias] = {'url': 'https://github.com/example/'+alias}
            self.declaration['services'][alias] = {'repository': alias, 'port': 8000}
        plan = self.resolve()
        self.assertEqual(len(plan['repositories']), 5)
        self.assertEqual(len(plan['services']), 5)

    def test_failed_clone_keeps_primary_and_failed_source_attribution(self):
        clone = Mock(side_effect=RuntimeError('Controlled clone failure'))
        with self.assertRaises(RuntimeError):
            resolve_repository_group(self.main, self.workspace, self.declaration, self.primary,
                                     clone, Mock(), self.state)
        self.assertEqual(self.state['repository'], 'backend')
        self.assertEqual(self.state['repositories'], {'main': self.primary})

    def test_primary_verification_does_not_replace_other_source_checks(self):
        self.declaration['verification'] = {'env': {'MODE': 'test'}}
        config = self.main/'.codereferee'
        config.mkdir()
        (config/'validation.yaml').write_text('version: 3\nverification:\n  env: {MODE: test}\n')
        plan = self.resolve()
        self.assertFalse(plan['sharedVerification'])

    def test_configuration_only_primary_can_group_two_application_sources(self):
        (self.main/'Dockerfile').unlink()
        self.declaration['repositories']['frontend'] = {'url': 'https://github.com/example/frontend.git'}
        self.declaration['services']['web']['repository'] = 'frontend'
        plan = self.resolve()
        self.assertEqual(len(plan['repositories']), 3)
        self.assertEqual(len(plan['services']), 2)
        self.assertEqual(plan['services']['web']['repositoryName'], 'frontend')

    def test_missing_source_is_configuration_failure(self):
        with self.assertRaises(ConfigurationRequired):
            resolve_repository_group(self.main, self.workspace, self.declaration, self.primary,
                Mock(side_effect=RuntimeError('fatal: repository not found')), Mock(), self.state)
        self.assertEqual(self.state['repository'], 'backend')

    def test_explicit_group_commit_pins_unmodified_additional_source(self):
        clone = Mock(side_effect=self.clone)
        runner = Mock(return_value=subprocess.CompletedProcess(['git'], 0, 'c'*40+'\n', ''))
        plan = resolve_repository_group(self.main, self.workspace, self.declaration, self.primary,
            clone, runner, self.state, repository_commits={'backend': 'c'*40})
        self.assertIsNone(clone.call_args.args[1])
        self.assertEqual(plan['repositories']['backend']['commitSha'], 'c'*40)

    def test_patch_and_group_commit_conflict_is_rejected_before_clone(self):
        clone = Mock()
        with self.assertRaises(ConfigurationRequired):
            resolve_repository_group(self.main, self.workspace, self.declaration, self.primary,
                clone, Mock(), self.state, repository_commits={'backend': 'b'*40},
                repository_patches={'backend': {'commitSha': 'c'*40, 'patchDiff': 'change'}})
        clone.assert_not_called()

    def test_unknown_repository_patch_alias_is_rejected_before_clone(self):
        clone = Mock()
        with self.assertRaises(ConfigurationRequired):
            resolve_repository_group(self.main, self.workspace, self.declaration, self.primary,
                clone, Mock(), self.state,
                repository_patches={'unknown': {'commitSha': 'c'*40, 'patchDiff': 'change'}})
        clone.assert_not_called()
