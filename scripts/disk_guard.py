"""Stop a request before it starts when the Docker host is nearly out of disk.

A full disk does not fail where it starts. npm, Maven, Gradle and docker build
each stop inside the repository's own step with ENOSPC, and the report then
reads as if the repository could not be built. Checking free space up front
turns that into an infrastructure error that names the cause.

Only the BuildKit build cache is pruned here. Images and volumes stay with
artifact_cleanup, which removes exactly what one request created.
"""
from __future__ import annotations

import os
import shutil
import subprocess

MIN_FREE_ENV = 'CODEREFEREE_MIN_FREE_DISK_GB'
# One Spring Boot request on the sandbox host needs about 3 GiB at its peak:
# the Maven repository in the check volume, the built image, and the tar that
# kind load writes before importing it. Keep headroom for a second copy of
# the image inside the kind node. Operators can raise it, or set 0 to disable.
DEFAULT_MIN_FREE_GB = 5
GIB = 1024 ** 3


class DiskLow(RuntimeError):
    def __init__(self, report: dict):
        self.report = report
        super().__init__(
            f"Sandbox host has {report['free_bytes'] / GIB:.1f} GiB free on {report['path']}; "
            f"a request needs at least {report['required_bytes'] / GIB:.1f} GiB. "
            'Remove unused Docker images and build cache on the sandbox host.')


def required_bytes() -> int:
    raw = os.getenv(MIN_FREE_ENV)
    try:
        value = float(raw) if raw not in (None, '') else DEFAULT_MIN_FREE_GB
    except ValueError:
        value = DEFAULT_MIN_FREE_GB
    return max(0, int(value * GIB))


def docker_root(run=subprocess.run) -> str:
    try:
        completed = run(['docker', 'info', '--format', '{{.DockerRootDir}}'],
                        capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return '/var/lib/docker'
    return completed.stdout.strip() if completed.returncode == 0 and completed.stdout.strip() else '/var/lib/docker'


def ensure_free_disk(run=subprocess.run, usage=shutil.disk_usage) -> dict:
    """Return what was measured, or raise DiskLow when space stays short.

    A host where free space cannot be measured is not blocked: the guard is
    there to name a cause, not to add a new way to fail.
    """
    required = required_bytes()
    if required == 0:
        return {'checked': False, 'reason': 'disabled'}
    path = docker_root(run)
    try:
        free = usage(path).free
    except OSError as exc:
        return {'checked': False, 'path': path, 'reason': str(exc)}
    report = {'checked': True, 'path': path, 'free_bytes': free, 'required_bytes': required,
              'build_cache_pruned': False}
    if free >= required:
        return report
    try:
        pruned = run(['docker', 'builder', 'prune', '--all', '--force'],
                     capture_output=True, text=True, timeout=300)
        report['build_cache_pruned'] = pruned.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    report['free_bytes_before_prune'] = free
    report['free_bytes'] = usage(path).free
    if report['free_bytes'] < required:
        raise DiskLow(report)
    return report
