import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from app.main import RepositoryValidationRequest, attach_repository_checks, deploy_repository
from app.executions import ExecutionStore
from scripts.validation_configuration import (ConfigurationRequired, apply_configuration,
    effective_configuration, validate_configuration)

TEXT = 'version: 1\nservice: {port: 8000}\n'


class ConfigurationUploadTests(unittest.TestCase):
    def test_request_accepts_aliases_and_normalizes_text(self):
        for key in ('validationYaml', 'validation_yaml'):
            request = RepositoryValidationRequest.model_validate({'repositoryUrl': 'https://github.com/example/app', key: '\ufeff' + TEXT.replace('\n', '\r\n')})
            self.assertEqual(request.validation_yaml, TEXT)

    def test_unsafe_or_incomplete_yaml_never_reaches_deployment(self):
        for value in [' ', '[]', 'service: {}', 'version: true\nservice: {}', 'version: 9',
                      'version: [', '!!python/object/apply:os.system [echo bad]',
                      'version: 1\nx: &loop {child: *loop}', TEXT + '\0', '가' * 22000]:
            with self.subTest(value=value[:30]), self.assertRaises((ValidationError, ValueError)):
                RepositoryValidationRequest(repositoryUrl='https://github.com/example/app', validationYaml=value)

    def test_incompatible_targets_are_not_silently_ignored(self):
        for extra in ({'deploymentProfile': 'quickbyte-demo'}, {'chaosMode': 'fixture'}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                RepositoryValidationRequest(repositoryUrl='https://github.com/example/app', validationYaml=TEXT, **extra)

    def test_override_and_refinement_preserve_input_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / '.codereferee/validation.yaml'
            config.parent.mkdir()
            config.write_text('version: 1\nservice: {port: 9000}\n')
            metadata = apply_configuration(root, TEXT)
            self.assertEqual(config.read_text(), TEXT)
            self.assertEqual(metadata['uploaded_sha256'], hashlib.sha256(TEXT.encode()).hexdigest())
            config.write_text(TEXT.replace('8000', '8001'))
            effective = effective_configuration(root, metadata)
            self.assertEqual(effective['uploaded_sha256'], metadata['uploaded_sha256'])
            self.assertNotEqual(effective['effective_sha256'], metadata['uploaded_sha256'])
            config.unlink()
            with self.assertRaises(ConfigurationRequired):
                effective_configuration(root, metadata)

    def test_symlink_cannot_redirect_the_config_write(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            try:
                (root / '.codereferee').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('Windows symlink privilege is unavailable')
            with self.assertRaises(ConfigurationRequired):
                apply_configuration(root, TEXT)
            self.assertFalse((Path(outside) / 'validation.yaml').exists())

    @patch('app.main.ensure_free_disk')
    @patch('app.main.run_bounded')
    def test_private_upload_file_is_forwarded_and_removed_even_on_failure(self, run, _disk):
        captured = []
        def execute(command, **kwargs):
            path = Path(command[command.index('--validation-file') + 1])
            self.assertEqual(path.read_text(encoding='utf-8'), TEXT)
            captured.append(path)
            raise RuntimeError('test failure')
        run.side_effect = execute
        with self.assertRaisesRegex(RuntimeError, 'test failure'):
            deploy_repository(RepositoryValidationRequest(repositoryUrl='https://github.com/example/app', requestId='upload', validationYaml=TEXT))
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].exists())

    def test_configuration_metadata_is_preserved_without_copying_yaml(self):
        result = {}
        metadata = {'source': 'request_upload', 'uploaded_sha256': 'a' * 64}
        attach_repository_checks(result, {'configurationInput': metadata})
        self.assertEqual(result['source']['configuration_input'], metadata)
        self.assertNotIn('validationYaml', json.dumps(result))

    def test_request_identity_preserves_legacy_requests_but_distinguishes_uploads(self):
        import hashlib
        request = RepositoryValidationRequest(repositoryUrl='https://github.com/example/app')
        previous = request.model_dump(mode='json')
        for key in ('validation_yaml', 'repository_patches', 'repository_commits'):
            previous.pop(key)
        legacy = hashlib.sha256(json.dumps(previous, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(ExecutionStore.identity(request), legacy)
        self.assertNotEqual(ExecutionStore.identity(request), ExecutionStore.identity(request.model_copy(update={'validation_yaml': TEXT})))


if __name__ == '__main__':
    unittest.main()
