import sys
import unittest
import tempfile
import os
import stat
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from deploy_repository import apply_patch as apply_repository_patch, clone, cleanup_workspace
from execution_plan import ConfigurationRequired


class RepositoryCloneTests(unittest.TestCase):
    @patch('deploy_repository.run')
    def test_patch_path_is_absolute_before_git_changes_working_directory(self, run):
        with tempfile.TemporaryDirectory() as temp:
            original = Path.cwd()
            try:
                os.chdir(temp)
                patch_file = Path('repair.diff')
                patch_file.write_text('patch', encoding='utf-8')
                apply_repository_patch(Path(temp) / 'clone', patch_file)
                self.assertTrue(Path(run.call_args.args[0][-1]).is_absolute())
            finally:
                os.chdir(original)

    def test_readonly_git_files_are_cleaned_with_exact_workspace_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            workspace = runtime / 'repository-test'
            workspace.mkdir()
            packed = workspace / 'pack-file'
            packed.write_text('temporary clone', encoding='utf-8')
            os.chmod(packed, stat.S_IREAD)
            with patch('deploy_repository.RUNTIME_ROOT', runtime):
                self.assertTrue(cleanup_workspace(workspace)['removed'])
                with self.assertRaises(ValueError): cleanup_workspace(runtime)
                with self.assertRaises(ValueError): cleanup_workspace(runtime / 'unrelated')

    @patch('deploy_repository.run')
    def test_clone_disables_windows_checkout_conversion_without_global_mutation(self, run):
        clone('https://github.com/example/application.git', 'main', Path('request-clone'))
        self.assertEqual(run.call_args.args[0], ['git', '-c', 'core.autocrlf=false',
            'clone', '--depth', '1', '--branch', 'main',
            'https://github.com/example/application.git', 'request-clone'])

    @patch('deploy_repository.run')
    def test_credential_url_is_rejected_before_git_execution(self, run):
        with self.assertRaises(ConfigurationRequired):
            clone('https://credential@github.com/example/application.git', None, Path('request-clone'))
        run.assert_not_called()
