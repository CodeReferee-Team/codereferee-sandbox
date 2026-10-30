"""A patch that only raises replicas or resources does not rerun build and tests.

2026-10-30: petclinic at 200 target users spent about five minutes per patch
round rebuilding and retesting code that the patch had not touched.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from deploy_repository import VALIDATION_CONFIG, only_scaling_changed, skipped_checks, validation_config


def git(repository, *args):
    subprocess.run(['git', *args], cwd=repository, check=True, capture_output=True, text=True)


class ScalingOnlyPatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = Path(self.directory.name)
        git(self.repository, 'init', '-q')
        (self.repository / 'app.py').write_text('print(1)\n')

    def commit(self):
        git(self.repository, 'add', '-A')
        git(self.repository, '-c', 'user.email=t@example.com', '-c', 'user.name=t', 'commit', '-qm', 'base')

    def write_config(self, text):
        path = self.repository / VALIDATION_CONFIG
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)

    def test_new_replicas_only_config_skips_checks(self):
        self.commit()
        before = validation_config(self.repository)
        self.write_config('version: 1\nservice:\n  replicas: 2\n')
        self.assertTrue(only_scaling_changed(self.repository, before))

    def test_changing_replicas_in_an_existing_config_skips_checks(self):
        self.write_config('version: 1\nservice:\n  dockerfile: Dockerfile\n  port: 8080\n  replicas: 1\n')
        self.commit()
        before = validation_config(self.repository)
        self.write_config('version: 1\nservice:\n  dockerfile: Dockerfile\n  port: 8080\n  replicas: 3\n'
                          '  resources:\n    limits:\n      memory: 1Gi\n')
        self.assertTrue(only_scaling_changed(self.repository, before))

    def test_code_change_reruns_checks(self):
        self.commit()
        before = validation_config(self.repository)
        self.write_config('version: 1\nservice:\n  replicas: 2\n')
        (self.repository / 'app.py').write_text('print(2)\n')
        self.assertFalse(only_scaling_changed(self.repository, before))

    def test_other_service_settings_rerun_checks(self):
        self.write_config('version: 1\nservice:\n  dockerfile: Dockerfile\n  port: 8080\n')
        self.commit()
        before = validation_config(self.repository)
        self.write_config('version: 1\nservice:\n  dockerfile: Dockerfile\n  port: 9090\n  replicas: 2\n')
        self.assertFalse(only_scaling_changed(self.repository, before))

    def test_verification_change_reruns_checks(self):
        self.commit()
        before = validation_config(self.repository)
        self.write_config('version: 1\nservice:\n  replicas: 2\nverification:\n  test: "true"\n')
        self.assertFalse(only_scaling_changed(self.repository, before))

    def test_invalid_yaml_reruns_checks(self):
        self.commit()
        before = validation_config(self.repository)
        self.write_config('version: 1\nservice: [\n')
        self.assertFalse(only_scaling_changed(self.repository, before))

    def test_skipped_report_says_why(self):
        report = skipped_checks({'version': 1, 'verification': {'test': ['make', 'test']}})
        self.assertEqual(report['outcome'], 'skipped')
        self.assertEqual(report['skipped_reason'], 'scaling_only_patch')
        self.assertTrue(report['verification_declared'])
        self.assertFalse(skipped_checks(None)['verification_declared'])


if __name__ == '__main__':
    unittest.main()
