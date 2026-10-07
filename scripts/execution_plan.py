"""Resolve declared or conservatively inferred repository deployment plans.

This parser never runs repository commands or reads a developer's environment.
Submitted paths stay inside the clone; dependencies are ephemeral, not production.
"""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

import yaml


class ConfigurationRequired(ValueError):
    pass


def inside(repository: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ConfigurationRequired('Paths must be repository-relative POSIX paths.')
    root = repository.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or '.git' in [part.lower() for part in Path(relative).parts]:
        raise ConfigurationRequired('Execution paths must stay inside the repository clone.')
    return path


def read_yaml(path: Path) -> dict:
    if path.stat().st_size > 256_000:
        raise ConfigurationRequired('Configuration exceeds 256 KB.')
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ConfigurationRequired('Configuration must be a YAML object.')
    return data


def resolve_plan(repository: Path, explicit_profile: str | None = None) -> dict[str, Any]:
    if explicit_profile:
        return {'source': 'request_profile', 'profile': explicit_profile}
    config_path = inside(repository, '.codereferee/validation.yaml')
    if config_path.is_file():
        data = read_yaml(config_path)
        if isinstance(data.get('deploymentProfile'), str):
            return {'source': 'repository_profile', 'profile': data['deploymentProfile']}
        if data.get('version') != 1 or not isinstance(data.get('service'), dict):
            raise ConfigurationRequired('validation.yaml requires version: 1 and service, or deploymentProfile.')
        if 'generatedDockerfile' in data['service']:
            raise ConfigurationRequired('Declare a checked-in Dockerfile, not generatedDockerfile.')
        plan = normalize_service(repository, data['service'])
        plan.update(source='repository_configuration', dependencies=normalize_dependencies(data.get('dependencies', {})))
        return plan

    for name in ('compose.yaml', 'compose.yml', 'docker-compose.yml', 'docker-compose.yaml'):
        path = inside(repository, name)
        if path.is_file():
            return compose_plan(repository, read_yaml(path), name)

    dockerfile = inside(repository, 'Dockerfile')
    if dockerfile.is_file():
        text = dockerfile.read_text(encoding='utf-8')
        ports = {int(p) for line in re.findall(r'^\s*EXPOSE\s+(.+)$', text, re.M | re.I)
                 for p in re.findall(r'\b(\d+)(?:/tcp)?\b', line)}
        if len(ports) != 1:
            raise ConfigurationRequired('Dockerfile must expose exactly one HTTP port, or declare service.port in validation.yaml.')
        plan = normalize_service(repository, {'port': ports.pop(), 'dockerfile': 'Dockerfile'})
        plan.update(source='dockerfile', dependencies={})
        return plan

    generated = stack_recipe(repository)
    if generated:
        plan = normalize_service(repository, generated)
        plan.update(source='stack_detection', dependencies={})
        return plan
    raise ConfigurationRequired('No unambiguous executable HTTP service found. Add .codereferee/validation.yaml; libraries and ambiguous multi-service projects are not auto-deployed.')


def normalize_service(repository: Path, service: dict) -> dict:
    allowed = {'dockerfile', 'buildContext', 'port', 'healthPath', 'command', 'args', 'env',
               'replicas', 'resources', 'generatedDockerfile'}
    unknown = set(service) - allowed
    if unknown:
        raise ConfigurationRequired('Unsupported service fields: ' + ', '.join(sorted(unknown)))
    port = service.get('port')
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigurationRequired('service.port must be an integer in 1..65535.')
    context = service.get('buildContext', '.')
    if not inside(repository, context).is_dir():
        raise ConfigurationRequired('Build context directory is missing.')
    dockerfile = service.get('dockerfile', 'Dockerfile')
    generated = service.get('generatedDockerfile')
    if not generated and not inside(repository, dockerfile).is_file():
        raise ConfigurationRequired('Dockerfile is missing; declare a buildable service.')
    replicas = service.get('replicas', 1)
    if isinstance(replicas, bool) or not isinstance(replicas, int) or not 1 <= replicas <= 4:
        raise ConfigurationRequired('replicas must be in 1..4 for this local runner.')
    path = service.get('healthPath', '/')
    if not isinstance(path, str) or not path.startswith('/') or len(path) > 512:
        raise ConfigurationRequired('healthPath must be a relative HTTP path starting with /.')
    result = dict(service, dockerfile=dockerfile, buildContext=context, port=port,
                  healthPath=path, replicas=replicas)
    for key in ('command', 'args'):
        if key in result and (not isinstance(result[key], list) or not result[key]
                              or any(not isinstance(value, str) for value in result[key])):
            raise ConfigurationRequired(key + ' must be a nonempty string argument array.')
    environment = result.get('env', {})
    if not isinstance(environment, dict) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', str(k)) for k in environment):
        raise ConfigurationRequired('env must be a mapping with valid environment names.')
    # No host interpolation, production credentials, or implicit env_file loading.
    if any(not isinstance(v, (str, int, bool)) or '${' in str(v) for v in environment.values()):
        raise ConfigurationRequired('env must contain explicit reproduction values, not host environment interpolation.')
    result['env'] = {k: str(v) for k, v in environment.items()}
    result['resources'] = normalize_resources(service.get('resources', {}))
    return result


def normalize_resources(resources: dict) -> dict:
    if not isinstance(resources, dict) or set(resources) - {'cpu', 'memory'}:
        raise ConfigurationRequired('resources supports cpu and memory only.')
    cpu = str(resources.get('cpu', '1000m'))
    memory = str(resources.get('memory', '512Mi'))
    if not re.fullmatch(r'(?:[1-4]|[1-9][0-9]{0,2}m|[1-3][0-9]{3}m|4000m)', cpu):
        raise ConfigurationRequired('cpu limit must be 1m..4000m or 1..4 cores.')
    match = re.fullmatch(r'(\d+)(Mi|Gi)', memory)
    if not match or not 32 <= int(match[1]) * (1024 if match[2] == 'Gi' else 1) <= 4096:
        raise ConfigurationRequired('memory limit must be 32Mi..4Gi.')
    cpu_m = int(cpu[:-1]) if cpu.endswith('m') else int(cpu) * 1000
    memory_mi = int(match[1]) * (1024 if match[2] == 'Gi' else 1)
    return {'limits': {'cpu': cpu, 'memory': memory},
            'requests': {'cpu': str(min(100, cpu_m)) + 'm', 'memory': str(min(128, memory_mi)) + 'Mi'}}


def normalize_dependencies(dependencies: dict) -> dict:
    if not isinstance(dependencies, dict):
        raise ConfigurationRequired('dependencies must be a mapping.')
    if len(dependencies) > 8:
        raise ConfigurationRequired('At most 8 ephemeral dependencies are supported.')
    for name, dependency in dependencies.items():
        if name == 'repository-api' or not re.fullmatch(r'[a-z][a-z0-9-]{0,40}', name) or not isinstance(dependency, dict):
            raise ConfigurationRequired('Invalid dependency declaration.')
        if dependency.get('type') not in {'redis', 'postgres'} or set(dependency) - {'type', 'probePath', 'probeSpec'}:
            raise ConfigurationRequired('Only ephemeral redis/postgres dependencies and explicit business probes are supported.')
    return dependencies


def compose_plan(repository: Path, data: dict, filename: str) -> dict:
    services = data.get('services', {})
    if not isinstance(services, dict):
        raise ConfigurationRequired('Compose services must be an object.')
    candidates = [(name, config) for name, config in services.items()
                  if isinstance(config, dict) and 'build' in config]
    if len(candidates) != 1:
        raise ConfigurationRequired('Compose requires exactly one buildable HTTP service; select the target in validation.yaml.')
    name, app = candidates[0]
    unsafe = {'privileged', 'network_mode', 'pid', 'devices', 'cap_add', 'env_file', 'secrets'} & set(app)
    if unsafe:
        raise ConfigurationRequired('Compose needs explicit safe reproduction settings for: ' + ', '.join(sorted(unsafe)))
    build = app['build']
    if not isinstance(build, (str, dict)) or (isinstance(build, dict) and set(build) - {'context', 'dockerfile'}):
        raise ConfigurationRequired('Unsupported Compose build configuration.')
    context = build if isinstance(build, str) else build.get('context', '.')
    dockerfile = 'Dockerfile' if isinstance(build, str) else build.get('dockerfile', 'Dockerfile')
    ports = []
    for value in app.get('ports', []):
        if isinstance(value, dict):
            ports.append(int(value['target']))
        elif re.fullmatch(r'(?:[0-9.:]+:)?\d+(?:/tcp)?', str(value)):
            ports.append(int(str(value).split(':')[-1].removesuffix('/tcp')))
        else:
            raise ConfigurationRequired('Unsupported Compose port mapping.')
    if not ports:
        ports = [int(v) for v in app.get('expose', [])]
    if len(set(ports)) != 1:
        raise ConfigurationRequired('Compose must declare exactly one target HTTP port.')
    environment = app.get('environment', {})
    if isinstance(environment, list):
        if any('=' not in item for item in environment):
            raise ConfigurationRequired('Compose cannot inherit host environment variables.')
        environment = dict(item.split('=', 1) for item in environment)
    service = {'port': ports[0], 'buildContext': context,
               'dockerfile': str(Path(context) / dockerfile), 'env': environment}
    # Compose entrypoint is exec-form only; command string means shell arguments,
    # so require explicit YAML instead of silently changing execution semantics.
    for source, dest in (('entrypoint', 'command'), ('command', 'args')):
        if source in app:
            if not isinstance(app[source], list):
                raise ConfigurationRequired('Compose command/entrypoint must use exec argument arrays.')
            service[dest] = app[source]
    dependencies = {}
    for dependency_name, config in services.items():
        if dependency_name == name:
            continue
        image = str(config.get('image', ''))
        kind = 'redis' if re.fullmatch(r'(?:docker.io/library/)?redis(?::[A-Za-z0-9_.-]+)?', image) else None
        if kind is None and re.fullmatch(r'(?:docker.io/library/)?postgres(?::[A-Za-z0-9_.-]+)?', image):
            kind = 'postgres'
        if kind is None or set(config) - {'image', 'ports', 'environment', 'volumes', 'restart', 'healthcheck'}:
            raise ConfigurationRequired('Compose dependency requires explicit validation.yaml configuration: ' + dependency_name)
        # Redis with custom passwords/commands cannot be inferred safely.
        if kind == 'redis' and config.get('environment'):
            raise ConfigurationRequired('Redis environment requires explicit reproduction configuration.')
        if kind == 'postgres':
            raise ConfigurationRequired('Compose PostgreSQL authentication requires explicit reproduction env templates in validation.yaml.')
        dependencies[dependency_name] = {'type': kind}
    plan = normalize_service(repository, service)
    plan.update(source='compose', sourceFile=filename,
                dependencies=normalize_dependencies(dependencies),
                warnings=['Compose host bind mounts and persistent volumes are not reused; dependencies are fresh ephemeral instances.'])
    return plan


def stack_recipe(repository: Path) -> dict | None:
    package_path = inside(repository, 'package.json')
    if package_path.is_file():
        package = json.loads(package_path.read_text(encoding='utf-8'))
        scripts = package.get('scripts', {})
        script = 'start' if isinstance(scripts.get('start'), str) else ('dev' if isinstance(scripts.get('dev'), str) else None)
        if script is None:
            return None
        install = 'npm ci' if inside(repository, 'package-lock.json').is_file() else 'npm install'
        recipe = ("FROM node:20-bookworm-slim\nWORKDIR /app\n"
                  "RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ && rm -rf /var/lib/apt/lists/*\n"
                  f"COPY . .\nRUN {install}\nENV PORT=3000 HOST=0.0.0.0\nEXPOSE 3000\nCMD {json.dumps(['npm', 'run', script], separators=(',', ':'))}\n")
        return {'port': 3000, 'generatedDockerfile': recipe}
    for entry in ('app.py', 'main.py', 'app/main.py'):
        path = inside(repository, entry)
        if not path.is_file():
            continue
        source = path.read_text(encoding='utf-8')
        if 'FastAPI(' in source:
            module = entry.removesuffix('.py').replace('/', '.')
            command = ['python', '-m', 'uvicorn', module + ':app', '--host', '0.0.0.0', '--port', '8000']
            extra = 'fastapi uvicorn'
        elif 'Flask(' in source and entry in {'app.py', 'main.py'}:
            command = ['python', '-m', 'flask', '--app', entry.removesuffix('.py'), 'run', '--host', '0.0.0.0', '--port', '8000']
            extra = 'flask'
        else:
            continue
        requirements = 'RUN pip install --no-cache-dir -r requirements.txt\n' if inside(repository, 'requirements.txt').is_file() else ''
        recipe = f'FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\n{requirements}RUN pip install --no-cache-dir {extra}\nEXPOSE 8000\nCMD {json.dumps(command)}\n'
        return {'port': 8000, 'generatedDockerfile': recipe}
    manifest = ''.join(path.read_text(encoding='utf-8') for path in
        (inside(repository, 'build.gradle'), inside(repository, 'build.gradle.kts'), inside(repository, 'pom.xml')) if path.is_file())
    if 'org.springframework.boot' in manifest or 'spring-boot' in manifest:
        java = '21' if re.search(r'(?:JavaLanguageVersion\.of\(|<java.version>\s*)21', manifest) else '17'
        if inside(repository, 'gradlew').is_file():
            builder = f'eclipse-temurin:{java}-jdk'
            build = "RUN sh ./gradlew bootJar --no-daemon && mkdir /output && find build/libs -maxdepth 1 -name '*.jar' ! -name '*-plain.jar' -exec cp {} /output/app.jar \\;\n"
        elif inside(repository, 'pom.xml').is_file():
            builder = f'maven:3.9.9-eclipse-temurin-{java}'
            build = "RUN mvn -B package -DskipTests && mkdir /output && find target -maxdepth 1 -name '*.jar' ! -name 'original-*' -exec cp {} /output/app.jar \\;\n"
        else:
            return None
        recipe = f'FROM {builder} AS builder\nWORKDIR /app\nCOPY . .\n{build}FROM eclipse-temurin:{java}-jre\nCOPY --from=builder /output/app.jar /app.jar\nENV SERVER_PORT=8080\nEXPOSE 8080\nENTRYPOINT ["java","-jar","/app.jar"]\n'
        return {'port': 8080, 'generatedDockerfile': recipe}
    return None


def render_plan(plan: dict, namespace: str, image: str) -> tuple[str, dict]:
    dependency_values = {}
    for name, dependency in plan['dependencies'].items():
        dependency_values[name] = {'host': name, 'port': '6379' if dependency['type'] == 'redis' else '5432',
            'username': 'codereferee', 'database': 'codereferee', 'password': secrets.token_hex(24)}
    environment = {}
    for name, value in plan['env'].items():
        for dependency, values in dependency_values.items():
            for key, replacement in values.items():
                value = value.replace('{{dependency.' + dependency + '.' + key + '}}', replacement)
        if '{{' in value or '}}' in value:
            raise ConfigurationRequired('Unknown reproduction environment template.')
        environment[name] = value
    labels = {'app.kubernetes.io/name': 'repository-api'}
    container = {'name': 'api', 'image': image, 'imagePullPolicy': 'IfNotPresent',
        'ports': [{'containerPort': plan['port']}], 'resources': plan['resources'],
        'env': [{'name': key, 'value': value} for key, value in environment.items()],
        'readinessProbe': {'httpGet': {'path': plan['healthPath'], 'port': plan['port']}, 'periodSeconds': 2, 'timeoutSeconds': 2},
        'startupProbe': {'httpGet': {'path': plan['healthPath'], 'port': plan['port']}, 'periodSeconds': 2, 'failureThreshold': 90, 'timeoutSeconds': 2}}
    for key in ('command', 'args'):
        if key in plan:
            container[key] = plan[key]
    objects = [{'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}},
        {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'repository-api', 'namespace': namespace},
         'spec': {'replicas': plan['replicas'], 'selector': {'matchLabels': labels},
                  'template': {'metadata': {'labels': labels}, 'spec': {'automountServiceAccountToken': False,
                    'containers': [container], 'terminationGracePeriodSeconds': 10}}}},
        {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'repository-api', 'namespace': namespace},
         'spec': {'selector': labels, 'ports': [{'port': plan['port'], 'targetPort': plan['port']}]}}]
    target_dependencies = {}
    for name, dependency in plan['dependencies'].items():
        kind = dependency['type']
        dep_port = 6379 if kind == 'redis' else 5432
        dep_image = 'redis:7-alpine' if kind == 'redis' else 'postgres:17-alpine'
        dep_labels = {'app.kubernetes.io/name': name}
        dep_container = {'name': kind, 'image': dep_image, 'ports': [{'containerPort': dep_port}],
                         'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}}}
        if kind == 'postgres':
            # Explicit reproduction values, never real credentials. Apps must use
            # these declared values; unknown external secret requirements are not inferred.
            dep_container['env'] = [{'name': 'POSTGRES_USER', 'value': 'codereferee'},
                {'name': 'POSTGRES_PASSWORD', 'value': dependency_values[name]['password']}, {'name': 'POSTGRES_DB', 'value': 'codereferee'}]
        objects += [
            {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': name, 'namespace': namespace},
             'spec': {'replicas': 1, 'selector': {'matchLabels': dep_labels}, 'template': {'metadata': {'labels': dep_labels},
                      'spec': {'automountServiceAccountToken': False, 'containers': [dep_container]}}}},
            {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': name, 'namespace': namespace},
             'spec': {'selector': dep_labels, 'ports': [{'port': dep_port, 'targetPort': dep_port}]}}]
        if dependency.get('probePath') or dependency.get('probeSpec'):
            key = 'redis' if kind == 'redis' else 'database'
            target_dependencies[key] = dict(dependency, deployment=name, container=kind,
                labelSelector='app.kubernetes.io/name=' + name)
    target = {'name': 'repository-api', 'deployment': 'repository-api', 'service': 'repository-api',
              'servicePort': plan['port'], 'labelSelector': 'app.kubernetes.io/name=repository-api',
              'probePath': plan['healthPath'], 'dependencies': target_dependencies}
    return yaml.safe_dump_all(objects, sort_keys=False), target
