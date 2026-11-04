"""Explicit single-repository service groups; no implicit choice of a business entry point."""
from __future__ import annotations

import re
import secrets
from pathlib import Path

import yaml

from execution_plan import (ConfigurationRequired, DEPENDENCY_PORTS, inside, normalize_dependencies,
                            normalize_service, render_plan, stack_recipe)


def workload_name(name: str) -> str:
    return 'cr-svc-' + name


def resolve_services(repository: Path, data: dict) -> dict:
    allowed = {'version', 'services', 'dependencies', 'entryService', 'targetService', 'probePath'}
    if set(data) - allowed:
        raise ConfigurationRequired('Unsupported version 2 configuration fields: ' + ', '.join(sorted(set(data) - allowed)))
    services = data.get('services')
    if not isinstance(services, dict) or not 2 <= len(services) <= 4:
        raise ConfigurationRequired('version 2 requires 2..4 named services.')
    dependencies = normalize_dependencies(data.get('dependencies', {}))
    normalized = {}
    for name, declaration in services.items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-z](?:[a-z0-9-]{0,29}[a-z0-9])?', name) or not isinstance(declaration, dict):
            raise ConfigurationRequired('Service names must be lowercase DNS labels of at most 31 characters.')
        if workload_name(name) in dependencies:
            raise ConfigurationRequired('A dependency name collides with a service workload.')
        service = dict(declaration)
        after = service.pop('dependsOn', [])
        if not isinstance(after, list) or any(not isinstance(n, str) or n not in services or n == name for n in after):
            raise ConfigurationRequired('dependsOn must name other declared services.')
        verification = service.pop('verificationDirectory', service.get('buildContext', '.'))
        if not inside(repository, verification).is_dir():
            raise ConfigurationRequired('verificationDirectory is missing.')
        if 'generatedDockerfile' in service:
            raise ConfigurationRequired('Declare a checked-in Dockerfile or a supported service stack.')
        context = inside(repository, service.get('buildContext', '.'))
        if 'dockerfile' not in service and not inside(repository, 'Dockerfile').is_file():
            generated = stack_recipe(context, port=service.get('port'))
            if generated:
                service['generatedDockerfile'] = generated['generatedDockerfile']
        normalized[name] = dict(normalize_service(repository, service), dependsOn=after,
                                verificationDirectory=verification)
    entry = data.get('entryService')
    target = data.get('targetService', entry)
    if not isinstance(entry, str) or entry not in normalized or not isinstance(target, str) or target not in normalized:
        raise ConfigurationRequired('entryService and targetService must name declared services.')
    path = data.get('probePath', normalized[entry]['healthPath'])
    if not isinstance(path, str) or not path.startswith('/') or len(path) > 512 or '\n' in path or '\r' in path:
        raise ConfigurationRequired('probePath must be an HTTP path starting with /.')
    order = []
    def visit(name, visiting):
        if name in visiting:
            raise ConfigurationRequired('Service dependsOn declarations contain a cycle.')
        if name in order:
            return
        for dependency in normalized[name]['dependsOn']:
            visit(dependency, visiting | {name})
        order.append(name)
    for name in normalized:
        visit(name, set())
    # Validate service references now, before building images. Dependency references are
    # checked by render_plan once the ephemeral credentials have been generated.
    for service in normalized.values():
        for value in service['env'].values():
            for kind, name, key in re.findall(r'{{(service|dependency)\.([^.{}]+)\.([^.{}]+)}}', value):
                table = normalized if kind == 'service' else dependencies
                fields = ('host', 'port', 'url') if kind == 'service' else ('host', 'port', 'username', 'database', 'password')
                if name not in table or key not in fields:
                    raise ConfigurationRequired('Unknown service/dependency reproduction template.')
            remaining = re.sub(r'{{(?:service|dependency)\.[^.{}]+\.[^.{}]+}}', '', value)
            if '{{' in remaining or '}}' in remaining:
                raise ConfigurationRequired('Unknown reproduction environment template.')
    return {'source': 'repository_services', 'services': normalized, 'serviceOrder': order,
            'entryService': entry, 'targetService': target, 'probePath': path,
            'dependencies': dependencies, 'warnings': []}


def render_services(plan: dict, namespace: str, images: dict[str, str]) -> tuple[str, dict, dict]:
    values = {name: {'host': name, 'port': str(DEPENDENCY_PORTS[d['type']]), 'username': 'codereferee',
                     'database': 'codereferee', 'password': secrets.token_hex(24)}
              for name, d in plan['dependencies'].items()}
    objects, targets, seen = [], {}, set()
    for name in plan['serviceOrder']:
        service = dict(plan['services'][name], dependencies=plan['dependencies'])
        environment = {}
        for key, value in service['env'].items():
            for peer, config in plan['services'].items():
                replacements = {'host': workload_name(peer), 'port': str(config['port']),
                                'url': f'http://{workload_name(peer)}:{config["port"]}'}
                for field, replacement in replacements.items():
                    value = value.replace('{{service.' + peer + '.' + field + '}}', replacement)
            environment[key] = value
        service['env'] = environment
        rendered, target = render_plan(service, namespace, images[name], workload=workload_name(name),
                                       dependency_values=values)
        targets[name] = dict(target, namespace=namespace, serviceName=name)
        for document in yaml.safe_load_all(rendered):
            identity = (document['kind'], document['metadata']['name'])
            if identity not in seen:
                seen.add(identity)
                objects.append(document)
    entry = targets[plan['entryService']]
    target = dict(targets[plan['targetService']], probeService=entry['service'],
                  probeServicePort=entry['servicePort'], probePath=plan['probePath'],
                  probeAcceptsClientErrors=False, entryService=plan['entryService'],
                  targetService=plan['targetService'], workloads=targets)
    return yaml.safe_dump_all(objects, sort_keys=False), target, targets


def verification_report(reports: dict) -> dict:
    """Keep service attribution; an undeclared test is never promoted to executed."""
    return {'detected_stack': 'multi_service', 'outcome': 'passed', 'services': reports,
            'verification_declared': all(r.get('verification_declared', False) for r in reports.values()),
            'test_execution': 'executed' if all(r.get('test_execution') == 'executed' for r in reports.values()) else 'partially_declared',
            'steps': [dict(step, service=name) for name, report in reports.items() for step in report.get('steps', [])],
            'test_count': sum(r.get('test_count') or 0 for r in reports.values())}
