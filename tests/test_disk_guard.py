"""A full sandbox disk is reported as an infrastructure error, not a repository failure.

2026-10-30: eight real repositories in a row stopped with ENOSPC inside npm,
Maven, Gradle or docker build, and each report read as a build failure of the
repository itself.
"""
import subprocess
import unittest
from collections import namedtuple
from unittest.mock import patch

from app.main import RepositoryValidationRequest, validate_repository
from scripts import disk_guard
from scripts.disk_guard import DiskLow, ensure_free_disk

Usage = namedtuple('Usage', 'total used free')
GIB = 1024 ** 3


def docker(prune_frees=0):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[:2] == ['docker', 'info']:
            return subprocess.CompletedProcess(command, 0, '/var/lib/docker\n', '')
        return subprocess.CompletedProcess(command, 0, '', '')
    return run, calls


class EnsureFreeDiskTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict('os.environ', {disk_guard.MIN_FREE_ENV: '5'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_enough_space_does_not_prune(self):
        run, calls = docker()
        report = ensure_free_disk(run, lambda _path: Usage(0, 0, 20 * GIB))
        self.assertTrue(report['checked'])
        self.assertEqual(report['path'], '/var/lib/docker')
        self.assertNotIn(['docker', 'builder', 'prune', '--all', '--force'], calls)

    def test_low_space_prunes_build_cache_and_goes_on_when_that_is_enough(self):
        run, calls = docker()
        free = iter([2 * GIB, 9 * GIB])
        report = ensure_free_disk(run, lambda _path: Usage(0, 0, next(free)))
        self.assertIn(['docker', 'builder', 'prune', '--all', '--force'], calls)
        self.assertTrue(report['build_cache_pruned'])
        self.assertEqual(report['free_bytes_before_prune'], 2 * GIB)
        self.assertEqual(report['free_bytes'], 9 * GIB)

    def test_space_still_short_after_prune_raises(self):
        run, _calls = docker()
        with self.assertRaises(DiskLow) as caught:
            ensure_free_disk(run, lambda _path: Usage(0, 0, 1 * GIB))
        self.assertIn('1.0 GiB free', str(caught.exception))
        self.assertEqual(caught.exception.report['required_bytes'], 5 * GIB)

    def test_images_and_volumes_are_never_pruned(self):
        run, calls = docker()
        with self.assertRaises(DiskLow):
            ensure_free_disk(run, lambda _path: Usage(0, 0, 0))
        flat = [' '.join(command) for command in calls]
        self.assertFalse([c for c in flat if 'image' in c or 'volume' in c or 'system' in c], flat)

    def test_unmeasurable_disk_does_not_block(self):
        run, _calls = docker()

        def missing(_path):
            raise FileNotFoundError('/var/lib/docker')
        self.assertFalse(ensure_free_disk(run, missing)['checked'])

    def test_zero_disables_the_guard(self):
        with patch.dict('os.environ', {disk_guard.MIN_FREE_ENV: '0'}):
            self.assertEqual(ensure_free_disk(*docker()[:1]), {'checked': False, 'reason': 'disabled'})

    def test_docker_unavailable_falls_back_to_default_root(self):
        def run(command, **_kwargs):
            raise FileNotFoundError('docker')
        self.assertEqual(disk_guard.docker_root(run), '/var/lib/docker')


class ValidateRepositoryDiskLowTests(unittest.TestCase):
    def test_disk_low_is_an_infrastructure_error_before_the_build(self):
        report = {'checked': True, 'path': '/var/lib/docker', 'free_bytes': GIB,
                  'required_bytes': 5 * GIB, 'build_cache_pruned': True}
        request = RepositoryValidationRequest(repositoryUrl='https://github.com/example/app',
                                              requestId='disk-low', chaosMode='suite_standard')
        with patch('app.main.ensure_free_disk', side_effect=DiskLow(report)), \
                patch('app.main.run_bounded') as run:
            result = validate_repository(request)
        run.assert_not_called()
        self.assertEqual(result['observationStatus'], 'infrastructure_error')
        self.assertEqual(result['infraError'], 'sandbox_disk_low')
        self.assertEqual(result['stoppedAt'], 'deploy')
        self.assertEqual(result['source']['disk_guard'], report)


if __name__ == '__main__':
    unittest.main()
