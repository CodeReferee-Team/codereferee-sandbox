import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from repository_patches import apply_repository_patches, validate_repository_patches
from execution_plan import ConfigurationRequired
from app.main import RepositoryValidationRequest, deploy_repository
from app.executions import ExecutionStore
from fastapi import HTTPException


class RepositoryPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.roots = {}
        self.snapshots = {}
        for name in ('main', 'backend', 'worker'):
            root = self.workspace / name
            root.mkdir()
            self.run_git(['git', 'init', '--initial-branch=main', str(root)])
            (root / 'app.py').write_text('VALUE = "old"\n')
            self.run_git(['git', 'add', '.'], cwd=root)
            self.run_git(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                          'commit', '-m', 'test: base'], cwd=root)
            self.roots[name] = root
            self.snapshots[name] = {'commitSha': self.run_git(['git', 'rev-parse', 'HEAD'], cwd=root).stdout.strip()}

    @staticmethod
    def run_git(args, **kwargs):
        return subprocess.run(args, check=True, capture_output=True, text=True, encoding='utf-8', **kwargs)

    def change(self, name, replacement='new'):
        root = self.roots[name]
        (root / 'app.py').write_text('VALUE = "' + replacement + '"\n')
        diff = self.run_git(['git', 'diff'], cwd=root).stdout
        self.run_git(['git', 'checkout', '--', 'app.py'], cwd=root)
        return {'commitSha': self.snapshots[name]['commitSha'], 'patchDiff': diff}

    def test_extra_patch_changes_only_its_repository(self):
        item = self.change('backend')
        state = {}
        metadata = apply_repository_patches(self.roots, self.snapshots, {'backend': item},
            self.workspace, self.run_git, state)
        self.assertIn('"new"', (self.roots['backend'] / 'app.py').read_text())
        self.assertIn('"old"', (self.roots['main'] / 'app.py').read_text())
        self.assertEqual(metadata['backend']['commitSha'], item['commitSha'])
        self.assertTrue(metadata['backend']['applied'])

    def test_all_checks_precede_primary_and_extra_mutation(self):
        primary = self.workspace / 'primary.diff'
        primary.write_text(self.change('main')['patchDiff'], encoding='utf-8')
        good = self.change('backend')
        bad = self.change('worker')
        bad['patchDiff'] = bad['patchDiff'].replace('"old"', '"missing"')
        with self.assertRaises(subprocess.CalledProcessError):
            apply_repository_patches(self.roots, self.snapshots, {'backend': good, 'worker': bad},
                self.workspace, self.run_git, {}, primary)
        for root in self.roots.values():
            self.assertIn('"old"', (root / 'app.py').read_text())

    def test_wrong_base_sha_rejected_before_application(self):
        item = self.change('backend')
        item['commitSha'] = 'f' * 40
        with self.assertRaises(ConfigurationRequired):
            apply_repository_patches(self.roots, self.snapshots, {'backend': item},
                self.workspace, self.run_git, {})
        self.assertIn('"old"', (self.roots['backend'] / 'app.py').read_text())

    def test_guard_rejects_protected_paths(self):
        for forbidden in ('.github/workflows/job.yml', '.git/config', '../outside'):
            item = {'commitSha': self.snapshots['backend']['commitSha'],
                    'patchDiff': '--- a/' + forbidden + '\n+++ b/' + forbidden + '\n'}
            with self.subTest(forbidden=forbidden), self.assertRaises(RuntimeError):
                apply_repository_patches(self.roots, self.snapshots, {'backend': item},
                    self.workspace, self.run_git, {})

    def test_git_decoded_escaped_ci_path_is_rejected(self):
        escaped = '.github\\057workflows/job.yml'
        diff = ('diff --git "a/' + escaped + '" "b/' + escaped + '"\nnew file mode 100644\n'
                '--- /dev/null\n+++ "b/' + escaped + '"\n@@ -0,0 +1 @@\n+unsafe\n')
        item = {'commitSha': self.snapshots['backend']['commitSha'], 'patchDiff': diff}
        with self.assertRaises(RuntimeError):
            apply_repository_patches(self.roots, self.snapshots, {'backend': item},
                self.workspace, self.run_git, {})
        self.assertFalse((self.roots['backend'] / '.github/workflows/job.yml').exists())


class RepositoryPatchContractTests(unittest.TestCase):
    def request(self, **kwargs):
        return RepositoryValidationRequest(repositoryUrl='https://github.com/example/frontend.git',
                                           requestId='patch-contract', **kwargs)

    def test_invalid_alias_sha_shape_and_size_are_rejected(self):
        valid = {'commitSha': 'a' * 40, 'patchDiff': 'test'}
        for value in ({'main': valid}, {'../bad': valid}, {'backend': dict(valid, commitSha='HEAD')},
                      {'backend': dict(valid, patchDiff='x' * 1_000_001)}):
            with self.subTest(value=list(value)), self.assertRaises(ValueError):
                self.request(repositoryPatches=value)

    def test_empty_new_fields_preserve_old_replay_identity(self):
        request = self.request()
        payload = request.model_dump(mode='json')
        payload.pop('repository_patches')
        payload.pop('repository_commits')
        previous = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(ExecutionStore.identity(request), previous)

    def test_patches_and_group_commits_change_replay_identity(self):
        baseline = ExecutionStore.identity(self.request())
        self.assertNotEqual(baseline, ExecutionStore.identity(self.request(repositoryCommits={'backend': 'a'*40})))
        self.assertNotEqual(baseline, ExecutionStore.identity(self.request(
            repositoryPatches={'backend': {'commitSha': 'a'*40, 'patchDiff': 'test'}})))

    def test_http_adapter_serializes_and_removes_private_patch_files(self):
        request = self.request(repositoryPatches={'backend': {'commitSha': 'a'*40, 'patchDiff': 'line\n'}},
                               repositoryCommits={'backend': 'a'*40})
        paths = []
        def execute(command, **kwargs):
            for option in ('--repository-patches-file', '--repository-commits-file'):
                path = Path(command[command.index(option)+1])
                paths.append(path)
                data = json.loads(path.read_text(encoding='utf-8'))
                self.assertIn('backend', data)
            return subprocess.CompletedProcess(command, 0, '{"target": {}}', '')
        with patch('app.main.ensure_free_disk'), patch('app.main.run_bounded', side_effect=execute):
            deploy_repository(request)
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_primary_and_additional_patch_share_total_size_limit(self):
        request = self.request(patchDiff='a'*600_000,
            repositoryPatches={'backend': {'commitSha': 'a'*40, 'patchDiff': 'b'*600_000}})
        with patch('app.main.ensure_free_disk'), patch('app.main.run_bounded') as run, self.assertRaises(HTTPException) as caught:
            deploy_repository(request)
        self.assertEqual(caught.exception.status_code, 422)
        run.assert_not_called()
