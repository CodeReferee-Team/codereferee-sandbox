import argparse
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import deploy_repository as deploy


class DeploymentImageCleanupTests(unittest.TestCase):
    def run_failure(self, stage, namespace_removed=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(request_id='cleanup-test', repository_url='https://github.com/example/app',
                branch=None, commit_sha=None, profile='test', patch_file=None, rollout_timeout_seconds=1)
            profile = {'manifestTemplate': 'template.yaml', 'target': {'deployment': 'api'}}
            if stage != 'missing_template':
                (root / 'template.yaml').write_text('manifest', encoding='utf-8')
            def clone(url, branch, destination):
                destination.mkdir()
                (destination / 'Dockerfile').write_text('FROM alpine', encoding='utf-8')
            def run(command, **kwargs):
                if stage == 'build' and command[0] == 'docker': raise RuntimeError('build failed')
                return subprocess.CompletedProcess(command, 0, 'abcdef1234567890', '')
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'RUNTIME_ROOT', root / '.runtime'), \
                 patch.object(deploy, 'parse_args', return_value=args), patch.object(deploy, 'clone', side_effect=clone), \
                 patch.object(deploy, 'load_profile', return_value=profile), patch.object(deploy, 'run', side_effect=run), \
                 patch.object(deploy, 'load_image_into_cluster', side_effect=RuntimeError('partial load') if stage == 'load' else None), \
                 patch.object(deploy, 'render_template', side_effect=RuntimeError('render failed') if stage == 'render' else None, return_value='manifest'), \
                 patch.object(deploy, 'apply', side_effect=RuntimeError('apply failed')), \
                 patch.object(deploy, 'delete_namespace', return_value=namespace_removed) as delete, \
                 patch.object(deploy, 'cleanup_request_image') as cleanup:
                with redirect_stdout(io.StringIO()):
                    try:
                        # The original executor raises; the generic executor
                        # returns a structured failure. Both must clean images.
                        self.assertEqual(deploy.main(), 1)
                    except RuntimeError:
                        pass
                self.assertEqual(cleanup.call_args.args[:2], ('codereferee/cleanup-test:abcdef123456', 'codereferee-cleanup-test'))
                return cleanup.call_args.kwargs['remove_from_kind'], delete.call_count

    def test_images_loaded_before_missing_template_are_removed_from_kind(self):
        self.assertEqual(self.run_failure('missing_template'), (True, 0))

    def test_images_loaded_before_render_failure_are_removed_from_kind(self):
        self.assertEqual(self.run_failure('render'), (True, 0))

    def test_partial_multi_node_load_is_also_cleaned(self):
        self.assertEqual(self.run_failure('load'), (True, 0))

    def test_build_failure_does_not_claim_images_were_loaded(self):
        self.assertEqual(self.run_failure('build'), (False, 0))

    def test_namespace_removal_failure_does_not_remove_running_pod_images(self):
        self.assertEqual(self.run_failure('apply', namespace_removed=False), (False, 1))
