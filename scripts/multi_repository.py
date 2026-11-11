"""Resolve explicitly declared repository sources for one service group."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from execution_control import JOURNAL_ENV, write_inventory
from execution_plan import ConfigurationRequired
from multi_service import resolve_services


def validate_repository_url(url):
    if (not isinstance(url, str) or
            not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', url) or
            '..' in url.split('/')[-2:]):
        raise ConfigurationRequired('Only public GitHub HTTPS repository URLs without credentials are supported.')


def validate_sources(data):
    sources = data.get('repositories')
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 4:
        raise ConfigurationRequired('version 3 requires 1..4 additional repositories; main is the submitted repository.')
    for name, source in sources.items():
        if (not isinstance(name, str) or name == 'main' or
                not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', name) or not isinstance(source, dict) or
                set(source) - {'url', 'branch', 'commitSha'}):
            raise ConfigurationRequired('Invalid repository name or declaration.')
        validate_repository_url(source.get('url'))
        branch = source.get('branch')
        if branch is not None and (not isinstance(branch, str) or not branch or len(branch) > 255 or
                branch.startswith('-') or any(ord(character) < 32 for character in branch)):
            raise ConfigurationRequired('Repository branch must be a nonempty ref without control characters.')
        commit = source.get('commitSha')
        if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40}', commit)):
            raise ConfigurationRequired('Repository commitSha must be a full lowercase 40-character SHA.')
    services = data.get('services')
    if not isinstance(services, dict) or not 2 <= len(services) <= 5:
        raise ConfigurationRequired('version 3 requires 2..5 services.')
    referenced = set()
    for service in services.values():
        if (not isinstance(service, dict) or not isinstance(service.get('repository', 'main'), str) or
                service.get('repository', 'main') not in {'main', *sources}):
            raise ConfigurationRequired('Each service must reference main or a declared repository.')
        referenced.add(service.get('repository', 'main'))
    if set(sources) - referenced:
        raise ConfigurationRequired('Every additional repository must supply at least one service.')
    return sources


def record_snapshot(name, snapshot):
    filename = os.getenv(JOURNAL_ENV)
    if filename:
        path = Path(filename)
        inventory = json.loads(path.read_text(encoding='utf-8'))
        inventory.setdefault('repository_snapshots', {})[name] = snapshot
        write_inventory(path, inventory)


def resolve_repository_group(repository: Path, workspace: Path, data: dict, primary: dict,
                             clone_repository, run, state: dict) -> dict:
    sources = validate_sources(data)
    roots = {'main': repository}
    snapshots = {'main': primary}
    state.update(stage='clone', repository_roots=roots, repositories=snapshots)
    record_snapshot('main', primary)
    pinned = json.loads(os.getenv('CODEREFEREE_REPOSITORY_SNAPSHOTS', '{}'))
    for name, source in sources.items():
        state['repository'] = name
        destination = workspace / 'sources' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        previous = pinned.get(name)
        if previous and previous['url'] != source['url']:
            raise ConfigurationRequired('Repository URL changed within a preparation retry.')
        commit = previous['commitSha'] if previous else source.get('commitSha')
        clone_repository(source['url'], None if commit else source.get('branch'), destination)
        if commit:
            run(['git', 'fetch', '--depth', '1', 'origin', commit], cwd=destination)
            run(['git', 'checkout', '--detach', commit], cwd=destination)
        resolved = run(['git', 'rev-parse', 'HEAD'], cwd=destination).stdout.strip()
        if not re.fullmatch(r'[a-f0-9]{40}', resolved):
            raise RuntimeError('Git did not return a valid repository commit SHA.')
        roots[name] = destination
        snapshots[name] = dict(source, commitSha=resolved)
        record_snapshot(name, snapshots[name])
    state.pop('repository', None)
    state.update(stage='detect', service=None)
    if 'verification' in data:
        from repository_checks import check_plan
        check_plan(repository)
    service_data = {key: value for key, value in data.items() if key not in {'repositories', 'verification'}}
    service_data['version'] = 2
    plan = resolve_services(repository, service_data, repository_roots=roots)
    plan.update(source='repository_group', repositories=snapshots)
    return plan
