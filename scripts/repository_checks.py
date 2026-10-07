"""Execute repository checks in bounded containers, never in the API host shell."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

from collect_baseline import kubectl_environment
from execution_plan import ConfigurationRequired, inside, read_yaml


class CheckFailure(RuntimeError):
    def __init__(self, report: dict):
        self.report = report
        super().__init__(report['steps'][-1].get('stderr') or 'Repository verification failed.')


def argument_array(value):
    if isinstance(value, str):
        if re.search(r'[;&|<>`\n]|\$\(', value):
            raise ConfigurationRequired('Verification commands use exec arguments, not shell expressions.')
        try:
            value = shlex.split(value)
        except ValueError as exc:
            raise ConfigurationRequired('Invalid quoted verification command.') from exc
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v for v in value):
        raise ConfigurationRequired('Verification command must be a nonempty string argument array.')
    return value


def check_plan(repository: Path) -> dict:
    config = inside(repository, '.codereferee/validation.yaml')
    data = read_yaml(config) if config.is_file() else {}
    declaration = data.get('verification', {})
    if not isinstance(declaration, dict) or set(declaration) - {'stack', 'test', 'build', 'env', 'workingDirectory'}:
        raise ConfigurationRequired('verification supports stack, test, build, env, workingDirectory.')
    working = declaration.get('workingDirectory', '.')
    if not inside(repository, working).is_dir():
        raise ConfigurationRequired('Verification workingDirectory does not exist.')
    root = inside(repository, working)
    candidates = []
    if inside(root, 'package.json').is_file(): candidates.append('node')
    if any(inside(root, p).is_file() for p in ('requirements.txt', 'pyproject.toml', 'setup.py', 'app.py', 'main.py')): candidates.append('python')
    if any(inside(root, p).is_file() for p in ('gradlew', 'build.gradle', 'build.gradle.kts')): candidates.append('gradle')
    if inside(root, 'pom.xml').is_file(): candidates.append('maven')
    stack = declaration.get('stack')
    if stack is None:
        if len(candidates) > 1:
            raise ConfigurationRequired('Multiple language manifests: declare verification.stack/workingDirectory.')
        stack = candidates[0] if candidates else None
    if stack is not None and stack not in {'node', 'python', 'gradle', 'maven'}:
        raise ConfigurationRequired('Unsupported verification stack.')
    env = declaration.get('env', {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k)
        or not isinstance(v, (str, int, bool)) or '${' in str(v) or '{{' in str(v) for k, v in env.items()):
        raise ConfigurationRequired('verification.env requires explicit non-secret reproduction values; host interpolation is disabled.')
    declared_test = declaration.get('test', data.get('test'))
    build, test, install = None, None, []
    image = None
    if stack == 'node':
        try:
            package = json.loads(inside(root, 'package.json').read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationRequired('Valid package.json is required for Node verification.') from exc
        scripts = package.get('scripts', {}) if isinstance(package, dict) else None
        if not isinstance(scripts, dict): raise ConfigurationRequired('package.json scripts must be an object.')
        image = 'node:20-bookworm'
        install = [['npm', 'ci' if inside(root, 'package-lock.json').is_file() else 'install']]
        if scripts.get('build'): build = ['npm', 'run', 'build']
        if scripts.get('test'): test = ['npm', 'test']
    elif stack == 'python':
        image = 'python:3.12-slim'
        if inside(root, 'requirements.txt').is_file(): install.append(['python', '-m', 'pip', 'install', '-r', 'requirements.txt'])
        if any(inside(root, p).is_file() for p in ('pyproject.toml', 'setup.py')):
            install.append(['python', '-m', 'pip', 'install', '.'])
        deps = data.get('testDependencies')
        if deps:
            if not inside(root, deps).is_file(): raise ConfigurationRequired('testDependencies file is missing.')
            install.append(['python', '-m', 'pip', 'install', '-r', deps])
        build = ['python', '-m', 'compileall', '-q', '.']
        if inside(root, 'tests').is_dir() or any(root.glob('test_*.py')):
            install.append(['python', '-m', 'pip', 'install', 'pytest'])
            test = ['python', '-m', 'pytest', '-q']
    elif stack in {'gradle', 'maven'}:
        manifest = ''.join(inside(root, name).read_text(encoding='utf-8') for name in
            ('build.gradle', 'build.gradle.kts', 'pom.xml') if inside(root, name).is_file())
        java = '21' if re.search(r'(?:JavaLanguageVersion\.of\(|<java.version>\s*)21', manifest) else '17'
        if stack == 'gradle':
            if not inside(root, 'gradlew').is_file(): raise ConfigurationRequired('Gradle wrapper is required for verification.')
            image = f'eclipse-temurin:{java}-jdk'
            build = ['sh', './gradlew', 'classes', '--no-daemon']
            test = ['sh', './gradlew', 'test', '--no-daemon'] if any(p.is_dir() for p in root.glob('**/src/test')) else None
        else:
            image = f'maven:3.9.9-eclipse-temurin-{java}'
            build = ['mvn', '-B', '-Dmaven.repo.local=/cache/maven', '-DskipTests', 'package']
            test = ['mvn', '-B', '-Dmaven.repo.local=/cache/maven', 'test'] if any(p.is_dir() for p in root.glob('**/src/test')) else None
    if 'build' in declaration: build = argument_array(declaration['build'])
    if declared_test is not None:
        if stack is None: raise ConfigurationRequired('Declared test needs verification.stack or a supported manifest.')
        test = argument_array(declared_test)
    return dict(stack=stack, image=image, install=install, build=build, test=test,
        declared=declared_test is not None, env={k: str(v) for k, v in env.items()}, workingDirectory=working)


def junit_test_count(repository: Path, stack: str) -> int | None:
    pattern = '**/build/test-results/test/TEST-*.xml' if stack == 'gradle' else '**/target/surefire-reports/TEST-*.xml'
    count = 0
    for path in repository.glob(pattern):
        checked = inside(repository, path.relative_to(repository).as_posix())
        if checked.stat().st_size > 2_000_000:
            return None
        try:
            element = ET.fromstring(checked.read_text(encoding='utf-8'))
            count += int(element.attrib.get('tests', 0))
        except (ET.ParseError, ValueError, OSError):
            return None
    return count


def verify_repository(repository: Path, cache: Path, request: str, *, timeout: int = 600) -> dict:
    # Validate before creating any resources. Linux package caches do not belong
    # on a slow Windows bind mount or in the submitted source/build context.
    plan = check_plan(repository)
    if not plan['image']:
        return _verify_repository(repository, '', request, timeout=timeout)
    volume = 'codereferee-check-cache-' + uuid4().hex
    environment = kubectl_environment()
    created = subprocess.run(['docker', 'volume', 'create', '--label', 'codereferee.temporary=true',
        '--label', 'codereferee.request-id=' + request, volume], text=True, capture_output=True,
        env=environment, timeout=30)
    if created.returncode:
        raise CheckFailure({'steps': [{'name': 'prepare', 'stderr': created.stderr[-2000:]}],
            'failed_step': 'prepare', 'exit_code': None, 'infrastructure_error': True,
            'outcome': 'failed', 'test_execution': 'not_attempted', 'verification_declared': plan['declared']})
    report = {}
    try:
        report = _verify_repository(repository, volume, request, timeout=timeout)
        return report
    except CheckFailure as failure:
        report = failure.report
        raise
    finally:
        try:
            removed = subprocess.run(['docker', 'volume', 'rm', volume], text=True, capture_output=True,
                env=environment, timeout=30)
            report['check_cache_cleanup'] = {'removed': removed.returncode == 0,
                'error': removed.stderr[-1000:] if removed.returncode else None}
        except (OSError, subprocess.TimeoutExpired):
            report['check_cache_cleanup'] = {'removed': False, 'error': 'Temporary check cache cleanup failed.'}


def _verify_repository(repository: Path, cache_name: str, request: str, *, timeout: int = 600) -> dict:
    plan = check_plan(repository)
    report = {'schema_version': 'sandbox-result.v1', 'detected_stack': plan['stack'] or 'unknown',
        'verification_declared': plan['declared'], 'test_execution': 'not_declared' if plan['test'] is None else 'pending',
        'outcome': 'not_attempted', 'failed_step': 'none', 'steps': []}
    if not plan['image']: return report
    environment = kubectl_environment()
    # Installed packages persist across check steps, but stay in the disposable
    # request cache instead of the API host or the app Docker build context.
    env = {'GRADLE_USER_HOME': '/cache/gradle', 'PYTHONUSERBASE': '/cache/python'}
    if plan['stack'] == 'python':
        env['PATH'] = '/cache/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    env.update(plan['env'])
    phases = [('install', command) for command in plan['install']]
    if plan['build']: phases.append(('build', plan['build']))
    if plan['test']: phases.append(('test', plan['test']))
    deadline = time.monotonic() + timeout
    for phase, arguments in phases:
        if plan['stack'] == 'python' and arguments[:4] == ['python', '-m', 'pip', 'install']:
            arguments = arguments[:4] + ['--user', '--disable-pip-version-check'] + arguments[4:]
        name = 'codereferee-check-' + uuid4().hex[:16]
        command = ['docker', 'run', '--rm', '--name', name, '--cpus', '2', '--memory', '2g',
            '--pids-limit', '512', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--mount', f'type=bind,source={repository.resolve()},target=/workspace',
            '--mount', f'type=volume,source={cache_name},target=/cache',
            '--workdir', '/workspace/' + plan['workingDirectory']]
        for key, value in env.items(): command.extend(['--env', key + '=' + value])
        command.extend([plan['image'], *arguments])
        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(command, text=True, capture_output=True, encoding='utf-8',
                errors='replace', env=environment, timeout=max(1, deadline-time.monotonic()))
            code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout or ''
            if isinstance(stdout, bytes): stdout = stdout.decode('utf-8', errors='replace')
            stderr = 'Repository check exceeded its bounded execution window.'
            code = None
        except OSError as exc:
            code, stdout, stderr = None, '', str(exc)
        finally:
            # A killed Docker CLI does not imply its container was stopped.
            if timed_out:
                try:
                    removed = subprocess.run(['docker', 'rm', '--force', name], capture_output=True,
                        text=True, env=environment, timeout=30)
                    report['check_container_cleanup'] = {'removed': removed.returncode == 0,
                        'error': removed.stderr[-1000:] if removed.returncode else None}
                except (OSError, subprocess.TimeoutExpired):
                    report['check_container_cleanup'] = {'removed': False, 'error': 'Check container cleanup failed.'}
        report['steps'].append({'name': phase, 'command': arguments, 'exit_code': code,
            'duration_ms': round((time.monotonic()-started)*1000), 'timed_out': timed_out,
            'stdout': stdout[-4000:], 'stderr': stderr[-4000:]})
        if code != 0:
            report.update(outcome='failed', failed_step=phase, exit_code=code,
                infrastructure_error=code is None or code == 125, timed_out=timed_out)
            if phase == 'test': report['test_execution'] = 'failed'
            elif report['test_execution'] == 'pending': report['test_execution'] = 'not_attempted'
            raise CheckFailure(report)
        if phase == 'test':
            report['test_execution'] = 'command_passed'
            if plan['stack'] in {'gradle', 'maven'}:
                count = junit_test_count(inside(repository, plan['workingDirectory']), plan['stack'])
                report['test_count'] = count
                if count == 0: report['test_execution'] = 'no_tests_collected'
    report['outcome'] = 'passed'
    return report
