"""Resolve declared or conservatively inferred repository deployment plans.

This parser never runs repository commands or reads a developer's environment.
Submitted paths stay inside the clone; dependencies are ephemeral, not production.
"""
from __future__ import annotations

import json
import re
import secrets
import shlex
from pathlib import Path
from typing import Any

import yaml

from local_databases import (FORWARDER, IMAGES as LOCAL_DATABASE_IMAGES, container_arguments,
                             container_environment, detect_local_databases)

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 호스트. Poetry 비패키지 감지만 빠진다.
    tomllib = None


class ConfigurationRequired(ValueError):
    pass


def inside(repository: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or '\\' in relative or Path(relative).is_absolute() or re.match(r'^[A-Za-z]:', relative):
        raise ConfigurationRequired('Paths must be repository-relative POSIX paths.')
    root = repository.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or '.git' in [part.lower() for part in Path(relative).parts]:
        raise ConfigurationRequired('Execution paths must stay inside the repository clone.')
    return path


# 레포가 선언한 Java 버전. Gradle toolchain은 정확히 같은 JDK가 있어야 빌드되므로
# 버전 카탈로그(gradle/libs.versions.toml)와 gradle.properties까지 본다(1chz realworld 실측).
JAVA_DECLARATIONS = re.compile(
    r'JavaLanguageVersion\.of\(\s*(\d+)|jvmToolchain\(\s*(\d+)|JavaVersion\.VERSION_(?:1_)?(\d+)'
    r'|(?:source|target)Compatibility\s*=\s*[\'"]?(?:1\.)?(\d+)'
    r'|<(?:java\.version|maven\.compiler\.(?:release|source|target)|release)>\s*(?:1\.)?(\d+)'
    r'|^\s*(?:java|javaVersion|java\.version|jdk)\s*=\s*"?(\d+)"?\s*$', re.MULTILINE)
SUPPORTED_JAVA = (17, 21, 25)
MAVEN_IMAGES = {'17': 'maven:3.9.9-eclipse-temurin-17', '21': 'maven:3.9.9-eclipse-temurin-21',
                '25': 'maven:3.9.11-eclipse-temurin-25'}


def java_release(root: Path) -> str:
    texts = [inside(root, name).read_text(encoding='utf-8', errors='replace') for name in
             ('build.gradle', 'build.gradle.kts', 'pom.xml', 'gradle/libs.versions.toml', 'gradle.properties')
             if inside(root, name).is_file()]
    declared = [int(v) for text in texts for match in JAVA_DECLARATIONS.findall(text) for v in match if v]
    wanted = max(declared, default=17)
    return str(next((v for v in SUPPORTED_JAVA if v >= wanted), SUPPORTED_JAVA[-1]))


def node_major(root: Path, package: dict | None = None) -> str:
    """레포가 요구하는 Node 주 버전. .nvmrc가 정확한 값이고, 없으면 engines.node의 하한을 쓴다.

    json-server처럼 engines가 22 이상을 요구하는 레포가 20에서 `bad option`으로 죽었다.
    지금까지의 기본값 20보다 낮추지는 않는다.
    """
    wanted = None
    for name in ('.nvmrc', '.node-version'):
        if inside(root, name).is_file():
            match = re.match(r'\s*v?(\d+)', inside(root, name).read_text(encoding='utf-8', errors='replace'))
            if match:
                wanted = int(match[1])
                break
    if wanted is None and isinstance(package, dict) and isinstance(package.get('engines'), dict):
        engine = package['engines'].get('node')
        if isinstance(engine, str):
            bounds = [int(m[1]) for part in engine.split('||') if (m := re.search(r'(\d+)', part))]
            wanted = min(bounds) if bounds else None
    return str(min(max(wanted or 20, 20), 26))


def _poetry_spec(name: str, constraint) -> str:
    """Poetry 제약을 pip 요구사항 한 줄로 바꾼다. ^와 ~는 Poetry 문서의 상한 규칙을 따른다."""
    extras = ''
    if isinstance(constraint, dict):
        if set(constraint) & {'git', 'path', 'url', 'file', 'source'}:
            raise ConfigurationRequired(f'Poetry dependency {name} comes from git/path/url; declare verification in validation.yaml.')
        if constraint.get('extras'):
            extras = '[' + ','.join(constraint['extras']) + ']'
        constraint = constraint.get('version', '*')
    if not isinstance(constraint, str):
        raise ConfigurationRequired(f'Poetry dependency {name} uses multiple constraints; declare verification in validation.yaml.')
    constraint = constraint.strip()
    match = re.fullmatch(r'([\^~])\s*(\d+(?:\.\d+)*)', constraint)
    if match:
        parts = [int(p) for p in match[2].split('.')]
        if match[1] == '^':
            index = next((i for i, p in enumerate(parts) if p), len(parts) - 1)
        else:
            index = 0 if len(parts) == 1 else 1
        upper = parts[:index + 1]
        upper[-1] += 1
        upper += [0] * (len(parts) - len(upper))
        constraint = f'>={match[2]},<' + '.'.join(map(str, upper))
    elif constraint == '*':
        constraint = ''
    elif re.fullmatch(r'\d+(?:\.\d+)*', constraint):
        constraint = '==' + constraint
    return name + extras + constraint


def poetry_requirements(root: Path, *, include_groups: bool) -> list[str] | None:
    """`package-mode = false`인 Poetry 프로젝트의 의존성. 패키지가 아니라 `pip install .`이 실패한다.

    fly-apps/hello-fastapi에서 실측했다. 그 밖의 pyproject는 None을 돌려 기존처럼 패키지로 설치한다.
    """
    path = inside(root, 'pyproject.toml')
    if tomllib is None or not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8'))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigurationRequired('pyproject.toml is not valid TOML.') from exc
    poetry = data.get('tool', {}).get('poetry', {})
    if not isinstance(poetry, dict) or poetry.get('package-mode', True) is not False:
        return None
    tables = [poetry.get('dependencies', {})]
    if include_groups:
        tables.append(poetry.get('dev-dependencies', {}))
        tables += [group.get('dependencies', {}) for group in poetry.get('group', {}).values() if isinstance(group, dict)]
    specs = [_poetry_spec(name, constraint) for table in tables if isinstance(table, dict)
             for name, constraint in table.items() if name.lower() != 'python']
    project = data.get('project', {})
    if isinstance(project, dict) and isinstance(project.get('dependencies'), list):
        specs += [spec for spec in project['dependencies'] if isinstance(spec, str)]
    return specs


# 엔트리 파일이 PORT를 읽지 않고 포트를 박아 둔 Node 앱. node-role-based-authorization-api는
# `NODE_ENV === 'production' ? 80 : 4000`이라 3000을 보던 프로브가 계속 실패했다(실측).
NODE_PORT_LINES = re.compile(r'\.listen\([^)\n]*|\bport\b\s*[:=][^;\n]*', re.IGNORECASE)


def node_entry_port(root: Path, package: dict, script: str) -> int | None:
    """엔트리 파일에 박힌 포트가 하나뿐일 때만 그 값을 쓴다. 둘 이상이면 고르지 않는다."""
    command = package.get('scripts', {}).get(script, '')
    named = re.search(r'(?:^|\s)(?:node|nodemon)\s+(?:--\S+\s+)*(\S+)', command)
    candidates = [named[1]] if named and named[1] != '.' else []
    if isinstance(package.get('main'), str):
        candidates.append(package['main'])
    candidates += ['server.js', 'app.js', 'index.js']
    for name in candidates:
        name = name.removeprefix('./')
        try:
            path = inside(root, name if name.endswith(('.js', '.cjs', '.mjs')) else name + '.js')
        except ConfigurationRequired:
            continue
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        # `NODE_ENV === 'production' ? (process.env.PORT || 80) : 4000`은 PORT를 production에서만 읽는다.
        # 우리는 NODE_ENV를 정하지 않으므로 그 줄의 PORT는 따르지 않는다(cornflourblue 예제 2건 실측).
        if any('process.env.PORT' in line and 'NODE_ENV' not in line for line in text.splitlines()):
            return None
        ports = {int(n) for line in NODE_PORT_LINES.findall(text) for n in re.findall(r'\b(\d{4,5})\b', line)
                 if 1024 <= int(n) <= 65535}
        return ports.pop() if len(ports) == 1 else None
    return None


def read_yaml(path: Path) -> dict:
    if path.stat().st_size > 256_000:
        raise ConfigurationRequired('Configuration exceeds 256 KB.')
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
    except yaml.YAMLError as exc:
        raise ConfigurationRequired('Configuration is not valid YAML.') from exc
    if not isinstance(data, dict):
        raise ConfigurationRequired('Configuration must be a YAML object.')
    return data


def exposed_tcp_ports(dockerfile: Path) -> set[int]:
    """Inspect only the final build stage; EXPOSE is a hint, not proof of HTTP."""
    ports = set()
    text = re.sub(r'\\\r?\n', ' ', dockerfile.read_text(encoding='utf-8'))
    for line in text.splitlines():
        tokens = line.split('#', 1)[0].split()
        if not tokens:
            continue
        if tokens[0].upper() == 'FROM':
            ports = set()
        elif tokens[0].upper() == 'EXPOSE':
            for token in tokens[1:]:
                if not re.fullmatch(r'\d+(?:/tcp)?', token):
                    raise ConfigurationRequired('Declare an explicit HTTP port for variable or non-TCP EXPOSE values.')
                ports.add(int(token.split('/')[0]))
    return ports


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
        if data['service'] and set(data['service']) <= OVERRIDE_FIELDS and 'dependencies' not in data:
            return detected_plan_with_overrides(repository, data['service'])
        if 'generatedDockerfile' in data['service']:
            raise ConfigurationRequired('Declare a checked-in Dockerfile, not generatedDockerfile.')
        service = dict(data['service'])
        context = inside(repository, service.get('buildContext', '.'))
        if 'artifactPath' in service:
            artifact = inside(repository, service.pop('artifactPath'))
            if not artifact.is_file() or artifact.suffix != '.jar' or not artifact.is_relative_to(context):
                raise ConfigurationRequired('artifactPath must identify an existing built JAR inside buildContext.')
            java = java_release(repository)
            service['generatedDockerfile'] = (f'FROM eclipse-temurin:{java}-jre\n'
                f'COPY {json.dumps([artifact.relative_to(context).as_posix(), "/app.jar"])}\n'
                f'ENV SERVER_PORT={service.get("port", 8080)}\n'
                'USER 10001:10001\nENTRYPOINT ["java","-jar","/app.jar"]\n')
        elif 'dockerfile' not in service and not inside(repository, 'Dockerfile').is_file():
            generated = stack_recipe(context, port=service.get('port'))
            if generated:
                service.update(generatedDockerfile=generated['generatedDockerfile'])
        plan = normalize_service(repository, service)
        plan.update(source='repository_configuration', dependencies=normalize_dependencies(data.get('dependencies', {})))
        return plan

    return detect_plan(repository)


# 배포 방식은 그대로 자동 감지에 맡기고 운영 값만 바꾸는 선언. 수정안 루프가 "복제본 1개라 여유가 없다"를
# 고칠 때 레포에 배포 파일이 없으면 이 파일 하나로 복제본 수를 올린다(spring-petclinic 부하 200명 Fail 실측).
# 포트·Dockerfile을 함께 적게 하면 감지가 하던 일을 손으로 다시 해야 하고 틀리기 쉽다.
OVERRIDE_FIELDS = {'replicas', 'resources'}


def detected_plan_with_overrides(repository: Path, overrides: dict) -> dict[str, Any]:
    plan = detect_plan(repository)
    replicas = overrides.get('replicas', plan['replicas'])
    if isinstance(replicas, bool) or not isinstance(replicas, int) or not 1 <= replicas <= 4:
        raise ConfigurationRequired('replicas must be in 1..4 for this local runner.')
    plan['replicas'] = replicas
    if 'resources' in overrides:
        plan['resources'] = normalize_resources(overrides['resources'])
    plan['overrides'] = sorted(overrides)
    return plan


def detect_plan(repository: Path) -> dict[str, Any]:
    for name in ('compose.yaml', 'compose.yml', 'docker-compose.yml', 'docker-compose.yaml'):
        path = inside(repository, name)
        if path.is_file():
            data = read_yaml(path)
            services = data.get('services', {}) if isinstance(data, dict) else None
            # A Compose file with no build target (DB-only, or pulling the published
            # app image) does not describe how to build this source tree.
            if isinstance(services, dict) and all(
                    isinstance(config, dict) and 'build' not in config for config in services.values()):
                break
            return compose_plan(repository, data, name)

    dockerfile = inside(repository, 'Dockerfile')
    if dockerfile.is_file():
        ports = exposed_tcp_ports(dockerfile)
        if len(ports) != 1:
            raise ConfigurationRequired('Dockerfile must expose exactly one HTTP port, or declare service.port in validation.yaml.')
        plan = normalize_service(repository, {'port': ports.pop(), 'dockerfile': 'Dockerfile'})
        plan.update(source='dockerfile', dependencies=local_dependencies(repository))
        return plan

    generated = stack_recipe(repository)
    if generated:
        plan = normalize_service(repository, generated)
        plan.update(source='stack_detection', dependencies=local_dependencies(repository))
        return plan
    raise ConfigurationRequired('No unambiguous executable HTTP service found. Add .codereferee/validation.yaml; libraries and ambiguous multi-service projects are not auto-deployed.')


def local_dependencies(repository: Path) -> dict:
    """설정 파일이 가리키는 로컬 DB. 선언이 없는 레포에만 쓴다(local_databases 참조)."""
    return {'local-' + kind: {'type': kind, 'local': database}
            for kind, database in detect_local_databases(repository).items()}


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
                  healthPath=path, replicas=replicas, healthPathDeclared='healthPath' in service)
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
        if dependency.get('type') not in {'redis', 'postgres', 'mysql'} or set(dependency) - {'type', 'probePath', 'probeSpec'}:
            raise ConfigurationRequired('Only ephemeral redis/postgres/mysql dependencies and explicit business probes are supported.')
    probe_kinds = [dependency['type'] == 'redis' for dependency in dependencies.values()
                   if dependency.get('probePath') or dependency.get('probeSpec')]
    if len(probe_kinds) != len(set(probe_kinds)):
        raise ConfigurationRequired('Declare at most one business-probed Redis and one database dependency for the current scenario contract.')
    return dependencies


def compose_plan(repository: Path, data: dict, filename: str) -> dict:
    services = data.get('services', {})
    if not isinstance(services, dict) or any(not isinstance(config, dict) for config in services.values()):
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
    mappings = app.get('ports', [])
    if not isinstance(mappings, list):
        raise ConfigurationRequired('Compose ports must be a list.')
    for value in mappings:
        if isinstance(value, dict):
            if value.get('protocol', 'tcp') != 'tcp' or isinstance(value.get('target'), bool) or not str(value.get('target', '')).isdigit():
                raise ConfigurationRequired('Compose HTTP port mappings need an integer TCP target.')
            ports.append(int(value['target']))
        elif re.fullmatch(r'(?:[0-9.:]+:)?\d+(?:/tcp)?', str(value)):
            ports.append(int(str(value).split(':')[-1].removesuffix('/tcp')))
        else:
            raise ConfigurationRequired('Unsupported Compose port mapping.')
    if not ports:
        exposed = app.get('expose', [])
        if not isinstance(exposed, list) or any(isinstance(v, bool) or not str(v).isdigit() for v in exposed):
            raise ConfigurationRequired('Compose expose must contain integer TCP ports.')
        ports = [int(v) for v in exposed]
    if len(set(ports)) != 1:
        raise ConfigurationRequired('Compose must declare exactly one target HTTP port.')
    compose_dockerfile = inside(repository, str(Path(context) / dockerfile))
    if not compose_dockerfile.is_file():
        raise ConfigurationRequired('Compose Dockerfile is missing; declare a buildable service.')
    declared_ports = exposed_tcp_ports(compose_dockerfile)
    if declared_ports and ports[0] not in declared_ports:
        raise ConfigurationRequired('Compose target port conflicts with Dockerfile EXPOSE; declare the reproduction port in validation.yaml.')
    environment = app.get('environment', {})
    if isinstance(environment, list):
        if any(not isinstance(item, str) or '=' not in item for item in environment):
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


def stack_recipe(repository: Path, port: int | None = None) -> dict | None:
    package_path = inside(repository, 'package.json')
    if package_path.is_file():
        try:
            package = json.loads(package_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as exc:
            raise ConfigurationRequired('package.json is not valid JSON.') from exc
        if not isinstance(package, dict) or not isinstance(package.get('scripts', {}), dict):
            raise ConfigurationRequired('package.json scripts must be an object.')
        scripts = package.get('scripts', {})
        script = 'start' if isinstance(scripts.get('start'), str) else ('dev' if isinstance(scripts.get('dev'), str) else None)
        if script is None:
            return None
        install = 'npm ci' if inside(repository, 'package-lock.json').is_file() else 'npm install'
        node_port = port if port is not None else (node_entry_port(repository, package, script) or 3000)
        if isinstance(node_port, bool) or not isinstance(node_port, int) or not 1 <= node_port <= 65535:
            raise ConfigurationRequired('service.port must be an integer in 1..65535.')
        recipe = (f"FROM node:{node_major(repository, package)}-bookworm-slim\nWORKDIR /app\n"
                  "RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ && rm -rf /var/lib/apt/lists/*\n"
                  f"COPY . .\nRUN {install}\nENV PORT={node_port} HOST=0.0.0.0\nEXPOSE {node_port}\nCMD {json.dumps(['npm', 'run', script], separators=(',', ':'))}\n")
        return {'port': node_port, 'generatedDockerfile': recipe}
    for entry in ('app.py', 'main.py', 'app/main.py'):
        path = inside(repository, entry)
        if not path.is_file():
            continue
        source = path.read_text(encoding='utf-8')
        python_port = port if port is not None else 8000
        if isinstance(python_port, bool) or not isinstance(python_port, int) or not 1 <= python_port <= 65535:
            raise ConfigurationRequired('service.port must be an integer in 1..65535.')
        if 'FastAPI(' in source:
            module = entry.removesuffix('.py').replace('/', '.')
            command = ['python', '-m', 'uvicorn', module + ':app', '--host', '0.0.0.0', '--port', str(python_port)]
            extra = 'fastapi uvicorn'
            app_env = ''
        elif 'Flask(' in source and entry in {'app.py', 'main.py'}:
            # --app은 Flask 2.2부터다. 레포가 고정한 Flask 1.x에서도 돌도록 FLASK_APP으로 넘긴다.
            command = ['python', '-m', 'flask', 'run', '--host', '0.0.0.0', '--port', str(python_port)]
            extra = 'flask'
            app_env = f'ENV FLASK_APP={entry.removesuffix(".py")}\n'
        else:
            continue
        requirements = 'RUN pip install --no-cache-dir -r requirements.txt\n' if inside(repository, 'requirements.txt').is_file() else ''
        poetry = poetry_requirements(repository, include_groups=False)
        if poetry:
            requirements += f'RUN pip install --no-cache-dir {shlex.join(poetry)}\n'
        # slim에는 컴파일러가 없어 소스 배포만 있는 패키지(multidict, PyICU)가 설치되지 않는다(microblog, cheat.sh 실측).
        tools = ('RUN apt-get update && apt-get install -y --no-install-recommends build-essential pkg-config '
                 '&& rm -rf /var/lib/apt/lists/*\n') if requirements else ''
        recipe = f'FROM python:3.12-slim\nWORKDIR /app\n{tools}COPY . .\n{requirements}RUN pip install --no-cache-dir {extra}\n{app_env}EXPOSE {python_port}\nCMD {json.dumps(command)}\n'
        return {'port': python_port, 'generatedDockerfile': recipe}
    manifest = ''.join(path.read_text(encoding='utf-8') for path in
        (inside(repository, 'build.gradle'), inside(repository, 'build.gradle.kts'), inside(repository, 'pom.xml')) if path.is_file())
    if 'org.springframework.boot' in manifest or 'spring-boot' in manifest:
        spring_port = port if port is not None else 8080
        if isinstance(spring_port, bool) or not isinstance(spring_port, int) or not 1 <= spring_port <= 65535:
            raise ConfigurationRequired('service.port must be an integer in 1..65535.')
        java = java_release(repository)
        if inside(repository, 'gradlew').is_file():
            builder = f'eclipse-temurin:{java}-jdk'
            build = "RUN sh ./gradlew bootJar --no-daemon && mkdir /output && find build/libs -maxdepth 1 -name '*.jar' ! -name '*-plain.jar' -exec cp {} /output/app.jar \\;\n"
        elif inside(repository, 'pom.xml').is_file():
            if '<modules>' in manifest and not inside(repository, 'src/main').is_dir():
                # spring-guides/tut-rest처럼 모듈마다 앱이 따로 있으면 어느 것을 띄울지 고르지 않는다.
                raise ConfigurationRequired('Maven multi-module project without a root application: '
                                            'add a Dockerfile or .codereferee/validation.yaml for the module to run.')
            builder = MAVEN_IMAGES[java]
            build = "RUN mvn -B package -DskipTests && mkdir /output && find target -maxdepth 1 -name '*.jar' ! -name 'original-*' -exec cp {} /output/app.jar \\;\n"
        else:
            return None
        recipe = f'FROM {builder} AS builder\nWORKDIR /app\nCOPY . .\n{build}FROM eclipse-temurin:{java}-jre\nCOPY --from=builder /output/app.jar /app.jar\nENV SERVER_PORT={spring_port}\nEXPOSE {spring_port}\nENTRYPOINT ["java","-jar","/app.jar"]\n'
        return {'port': spring_port, 'generatedDockerfile': recipe}
    return None


DEPENDENCY_PORTS = {'redis': 6379, 'postgres': 5432, 'mysql': 3306, 'mongo': 27017}


def render_plan(plan: dict, namespace: str, image: str) -> tuple[str, dict]:
    dependency_values = {}
    for name, dependency in plan['dependencies'].items():
        dependency_values[name] = {'host': name, 'port': str(DEPENDENCY_PORTS[dependency['type']]),
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
    accepts_client_errors = not plan.get('healthPathDeclared', True)
    if accepts_client_errors:
        # 헬스 경로를 정하지 않은 레포는 GET /가 401·404여도 기동한 것이다(Spring Security, 루트 라우트 없는 API).
        # 쿠버네티스 httpGet 프로브는 200~399만 인정하므로 포트가 열렸는지로 기동을 보고,
        # HTTP 응답 여부(5xx 미만)는 관측 프로브가 따로 잰다.
        for probe in ('readinessProbe', 'startupProbe'):
            container[probe] = dict(container[probe], tcpSocket={'port': plan['port']})
            del container[probe]['httpGet']
    for key in ('command', 'args'):
        if key in plan:
            container[key] = plan[key]
    containers = [container]
    forwards = [f'{DEPENDENCY_PORTS[d["type"]]}:{name}:{DEPENDENCY_PORTS[d["type"]]}'
                for name, d in plan['dependencies'].items() if d.get('local')]
    if forwards:
        # 레포 설정은 localhost의 DB를 본다. 앱 Pod 안에서 그 포트를 DB Service로 넘긴다.
        containers.append({'name': 'local-db-forward', 'image': 'python:3.12-alpine', 'imagePullPolicy': 'IfNotPresent',
            'command': ['python', '-c', FORWARDER, *forwards],
            'resources': {'requests': {'cpu': '10m', 'memory': '16Mi'}, 'limits': {'cpu': '200m', 'memory': '64Mi'}}})
    objects = [{'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}},
        {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'repository-api', 'namespace': namespace},
         'spec': {'replicas': plan['replicas'], 'selector': {'matchLabels': labels},
                  'template': {'metadata': {'labels': labels}, 'spec': {'automountServiceAccountToken': False,
                    'containers': containers, 'terminationGracePeriodSeconds': 10}}}},
        {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'repository-api', 'namespace': namespace},
         'spec': {'selector': labels, 'ports': [{'port': plan['port'], 'targetPort': plan['port']}]}}]
    target_dependencies = {}
    for name, dependency in plan['dependencies'].items():
        kind = dependency['type']
        dep_port = DEPENDENCY_PORTS[kind]
        dep_image = {'redis': 'redis:7-alpine', 'postgres': 'postgres:17-alpine', 'mysql': 'mysql:8.4'}.get(kind)
        dep_labels = {'app.kubernetes.io/name': name}
        dep_container = {'name': kind, 'image': dep_image, 'ports': [{'containerPort': dep_port}],
                         'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}}}
        local = dependency.get('local')
        if local:
            # 레포에 적힌 개발용 계정 그대로. 생성한 비밀번호를 쓰면 앱 설정과 맞지 않는다.
            dep_container['image'] = LOCAL_DATABASE_IMAGES[kind]
            environment = container_environment(local)
            if environment:
                dep_container['env'] = [{'name': key, 'value': value} for key, value in environment.items()]
            arguments = container_arguments(local)
            if arguments:
                dep_container['args'] = arguments
            if kind in {'mysql', 'mongo'}:
                dep_container['resources']['requests']['memory'] = '128Mi'
                dep_container['resources']['limits']['memory'] = '512Mi'
        elif kind == 'postgres':
            # Explicit reproduction values, never real credentials. Apps must use
            # these declared values; unknown external secret requirements are not inferred.
            dep_container['env'] = [{'name': 'POSTGRES_USER', 'value': 'codereferee'},
                {'name': 'POSTGRES_PASSWORD', 'value': dependency_values[name]['password']}, {'name': 'POSTGRES_DB', 'value': 'codereferee'}]
        elif kind == 'mysql':
            dep_container['resources']['requests']['memory'] = '128Mi'
            dep_container['resources']['limits']['memory'] = '512Mi'
            dep_container['env'] = [{'name': 'MYSQL_USER', 'value': 'codereferee'},
                {'name': 'MYSQL_PASSWORD', 'value': dependency_values[name]['password']},
                {'name': 'MYSQL_DATABASE', 'value': 'codereferee'},
                {'name': 'MYSQL_ROOT_PASSWORD', 'value': secrets.token_hex(24)}]
        dep_container['readinessProbe'] = {'tcpSocket': {'port': dep_port}, 'periodSeconds': 2}
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
              'probePath': plan['healthPath'], 'dependencies': target_dependencies,
              'probeAcceptsClientErrors': accepts_client_errors}
    return yaml.safe_dump_all(objects, sort_keys=False), target
