"""Validate and apply commit-bound patches to request-owned source checkouts."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

try:
    from .execution_plan import ConfigurationRequired
except ImportError:
    from execution_plan import ConfigurationRequired


MAX_PATCH_BYTES = 1_000_000


def validate_repository_commits(value):
    if not isinstance(value, dict) or len(value) > 4 or any(
            not isinstance(name, str) or name == 'main' or
            not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', name) or not isinstance(commit, str) or
            not re.fullmatch(r'[a-f0-9]{40}', commit) for name, commit in value.items()):
        raise ConfigurationRequired('repositoryCommits requires additional aliases and full lowercase commit SHAs.')
    return value


def validate_repository_patches(value):
    if not isinstance(value, dict) or len(value) > 4:
        raise ConfigurationRequired('repositoryPatches must map at most four additional repository aliases.')
    for name, item in value.items():
        if (not isinstance(name, str) or name == 'main' or
                not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', name) or not isinstance(item, dict) or
                set(item) != {'commitSha', 'patchDiff'} or
                not isinstance(item['commitSha'], str) or
                not re.fullmatch(r'[a-f0-9]{40}', item['commitSha']) or
                not isinstance(item['patchDiff'], str) or not item['patchDiff'].strip()):
            raise ConfigurationRequired('Repository patches require an additional alias, commitSha and nonempty patchDiff.')
    if sum(len(item['patchDiff'].encode('utf-8')) for item in value.values()) > MAX_PATCH_BYTES:
        raise ConfigurationRequired('Combined repository patches exceed the 1 MiB Sandbox limit.')
    return value


def check_patch_file(patch_file: Path) -> str:
    if not patch_file.is_file():
        raise RuntimeError('Patch file was not found.')
    if patch_file.stat().st_size > MAX_PATCH_BYTES:
        raise RuntimeError('Patch exceeds the 1 MiB Sandbox limit.')
    text = patch_file.read_text(encoding='utf-8')
    if any(value in text for value in ('.github/workflows/', '.git/', '../')):
        raise RuntimeError('Patch changes a protected path.')
    return text


def check_patch_targets(repository, patch_file, run):
    # Git decodes quoted and octal-escaped diff paths for us.
    output = run(['git', 'apply', '--numstat', '-z', str(patch_file)], cwd=repository).stdout
    for entry in output.split('\0'):
        if not entry:
            continue
        name = entry.split('\t', 2)[-1].replace('\\', '/')
        parts = name.casefold().split('/')
        if (name.startswith('/') or re.match(r'^[a-zA-Z]:', name) or '..' in parts or '.git' in parts or
                '.github/workflows' in '/'.join(parts)):
            raise RuntimeError('Patch changes a protected path.')


def apply_repository_patches(roots, snapshots, patches, workspace, run, state, primary_patch=None):
    pending = []
    if primary_patch is not None:
        pending.append(('main', Path(primary_patch).resolve()))
    for name, patch in patches.items():
        state['repository'] = name
        if name not in roots:
            raise ConfigurationRequired('Patch references an undeclared repository: ' + name)
        if snapshots[name]['commitSha'] != patch['commitSha']:
            raise ConfigurationRequired('Patch base commit does not match repository ' + name)
        path = workspace / ('repository-patch-' + name + '.diff')
        path.write_text(patch['patchDiff'], encoding='utf-8', newline='\n')
        pending.append((name, path))
    if sum(path.stat().st_size for _, path in pending) > MAX_PATCH_BYTES:
        raise ConfigurationRequired('Combined patches exceed the 1 MiB Sandbox limit.')
    state['stage'] = 'patch'
    for name, path in pending:
        state['repository'] = name
        check_patch_file(path)
        run(['git', 'apply', '--check', str(path)], cwd=roots[name])
        check_patch_targets(roots[name], path, run)
    metadata = {}
    for name, path in pending:
        state['repository'] = name
        run(['git', 'apply', str(path)], cwd=roots[name])
        metadata[name] = {'commitSha': snapshots[name]['commitSha'],
                          'patchSha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'applied': True}
    state.pop('repository', None)
    state['repository_patches'] = metadata
    return metadata
