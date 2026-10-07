import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from deploy_repository import clone
from execution_plan import ConfigurationRequired


class RepositoryCloneTests(unittest.TestCase):
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
