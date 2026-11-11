"""Recover the exact resources recorded before a request creates them."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

try:
    from .artifact_cleanup import cleanup_request_image
    from .collect_baseline import kubectl_environment
    from .namespace_cleanup import delete_namespace_confirmed
except ImportError:
    from artifact_cleanup import cleanup_request_image
    from collect_baseline import kubectl_environment
    from namespace_cleanup import delete_namespace_confirmed


ROOT = Path(__file__).resolve().parents[1]


def create_inventory(directory: Path, resource_id: str) -> Path:
    if not re.fullmatch(r'exec-[a-f0-9]{24}', resource_id):
        raise ValueError('Invalid execution resource identity.')
    path = directory / (resource_id + '.json')
    path.write_text(json.dumps({'resource_id': resource_id, 'phase': 'starting',
        'images': [], 'loaded_images': [], 'containers': [], 'volumes': [], 'namespaces': [],
        'observation_namespaces': []}), encoding='utf-8')
    return path


def cleanup_workspace(workspace: Path, runtime_root: Path) -> dict:
    runtime = runtime_root.resolve()
    resolved = workspace.resolve()
    if resolved.parent != runtime or not re.fullmatch(r'repository-[a-z0-9-]{1,45}', resolved.name):
        raise ValueError('Refusing cleanup outside an exact request workspace.')

    def retry_readonly(function, path, exc_info):
        candidate = Path(path)
        if not candidate.resolve().is_relative_to(resolved):
            raise ValueError('Refusing permissions change outside request workspace.')
        os.chmod(candidate, candidate.stat().st_mode | stat.S_IWRITE)
        function(path)

    try:
        if workspace.exists():
            shutil.rmtree(workspace, onerror=retry_readonly)
        return {'removed': not workspace.exists(), 'error': None}
    except (OSError, ValueError) as exc:
        return {'removed': False, 'error': str(exc)[-1000:]}


def recover_inventory(path: Path, *, timeout_seconds: float = 90) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    resource_id = data['resource_id']
    if path.name != resource_id + '.json' or not re.fullmatch(r'exec-[a-f0-9]{24}', resource_id):
        raise ValueError('Invalid recovery inventory identity.')
    namespace = 'codereferee-' + resource_id
    deadline = time.monotonic() + timeout_seconds
    report = {'namespace_removed': False, 'images': [], 'errors': []}

    def remove(arguments):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Recovery budget exhausted.')
        result = subprocess.run(arguments, capture_output=True, text=True, env=kubectl_environment(),
                                timeout=min(15, remaining))
        absent = any(term in result.stderr.lower() for term in ('no such container', 'no such volume'))
        if result.returncode and not absent:
            report['errors'].append('Temporary resource removal failed.')

    try:
        for container in data['containers']:
            if not re.fullmatch(r'codereferee-check-[a-z0-9-]{8,80}', container):
                raise ValueError('Invalid check container identity.')
            remove(['docker', 'rm', '--force', container])
        for volume in data['volumes']:
            if not re.fullmatch(r'codereferee-check-cache-[a-f0-9]{32}', volume):
                raise ValueError('Invalid check volume identity.')
            remove(['docker', 'volume', 'rm', volume])
        report['namespace_removed'] = namespace not in data['namespaces']
        if not report['namespace_removed']:
            report['namespace_removed'] = delete_namespace_confirmed(namespace,
                timeout_seconds=max(0, min(60, deadline-time.monotonic())))
        for observation in data.get('observation_namespaces', []):
            if not re.fullmatch(r'codereferee-metrics-[a-f0-9]{16}', observation):
                raise ValueError('Invalid observation namespace identity.')
            if not delete_namespace_confirmed(observation, timeout_seconds=max(0, deadline-time.monotonic())):
                report['errors'].append('Observation namespace cleanup is pending.')
        for image in data['images']:
            cleaned = cleanup_request_image(image, namespace,
                remove_from_kind=report['namespace_removed'] and image in data['loaded_images'],
                deadline=deadline)
            report['images'].append(cleaned)
            report['errors'].extend(cleaned['errors'])
    except (OSError, subprocess.TimeoutExpired, TimeoutError, ValueError):
        report['errors'].append('Recovery could not finish within its bounded execution window.')
    report['workspace_cleanup'] = cleanup_workspace(ROOT / '.runtime' / ('repository-' + resource_id), ROOT / '.runtime')
    report['complete'] = report['namespace_removed'] and report['workspace_cleanup']['removed'] and not report['errors']
    return report
