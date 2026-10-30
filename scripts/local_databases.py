"""레포 설정이 로컬 DB를 가리키면 그 DB를 같은 접속 정보로 임시로 띄운다.

실제 레포 배치(2026-10-30)에서 판정 불가 32건 중 10건이 DB가 없어서였다. 열 건 모두
`localhost`/`127.0.0.1`과 레포에 적힌 개발용 계정을 그대로 썼다(application.properties의
`jdbc:mysql://localhost:3306/testdb` + root/123456, config.json의 `mongodb://localhost/...`,
conf/app.ini의 `Host = 127.0.0.1:3306`). 그래서 앱 설정을 바꾸지 않고 레포가 기대하는 DB를
그대로 재현한다. 접속 주소를 고쳐 주는 방식은 스택마다 덮어쓰는 방법이 달라 일부만 살린다.

- 레포에 적힌 계정만 쓴다. 운영 비밀값이 아니라 개발용 값이고, DB는 요청마다 새로 만들고 지운다.
- 호스트가 localhost/127.0.0.1일 때만 붙인다. 원격 주소는 레포가 우리에게 기대하는 DB가 아니다.
- 자리표시자(`${DB_PASSWORD}`)는 기본값이 있을 때만 쓴다. 값을 지어내지 않는다.
- 같은 종류는 하나만. 둘이면 앞의 것을 쓴다(포트가 겹친다).
"""
from __future__ import annotations

import configparser
import json
import re
from pathlib import Path

import yaml

LOCAL_HOSTS = {'localhost', '127.0.0.1'}
DEFAULT_PORTS = {'mysql': 3306, 'postgres': 5432, 'mongo': 27017, 'redis': 6379}
# 로컬에서 띄운 DB라는 표시. render_plan과 검사 단계가 이 값으로 계정·이미지를 고른다.
IMAGES = {'mysql': 'mysql:8.0', 'postgres': 'postgres:17-alpine', 'mongo': 'mongo:7', 'redis': 'redis:7-alpine'}
SKIPPED_DIRECTORIES = {'node_modules', '.git', 'target', 'build', 'dist', 'vendor', '.venv', 'venv', 'test', 'tests'}
JDBC = re.compile(r'jdbc:(mysql|mariadb|postgresql)://([^/:?]+)(?::(\d+))?/([A-Za-z0-9_\-]+)')
MONGO_URI = re.compile(r'mongodb://(?:([^:@/\s]+)(?::([^@/\s]*))?@)?([^/:?\s,]+)(?::(\d+))?/([A-Za-z0-9_\-]+)')
REDIS_URI = re.compile(r'redis://(?::([^@/\s]*)@)?([^/:?\s]+)(?::(\d+))?')


def _resolved(value) -> str | None:
    """`${NAME:default}`는 기본값으로, 기본값 없는 자리표시자는 알 수 없음(None)으로 본다."""
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r'\$\{[^:}]+:([^}]*)\}', text)
    if match:
        return match[1]
    return None if '${' in text else text


def _walk(root: Path, patterns: tuple[str, ...], depth: int = 3):
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            relative = path.relative_to(root)
            if (path.is_file() and len(relative.parts) <= depth + 1 and path.stat().st_size < 256_000
                    and not SKIPPED_DIRECTORIES & set(relative.parts[:-1])):
                yield path


def _flatten(data, prefix=''):
    if isinstance(data, dict):
        for key, value in data.items():
            yield from _flatten(value, f'{prefix}{key}.')
    else:
        yield prefix.rstrip('.'), data


def _spring_properties(root: Path) -> list[tuple[Path, dict]]:
    found = []
    for path in _walk(root, ('src/main/resources/application*.properties', 'src/main/resources/application*.yml',
                             'src/main/resources/application*.yaml'), depth=4):
        text = path.read_text(encoding='utf-8', errors='replace')
        if path.suffix == '.properties':
            values = {}
            for line in text.splitlines():
                match = re.match(r'\s*([A-Za-z0-9_.\-\[\]]+)\s*[=:]\s*(.*?)\s*$', line)
                if match and not line.lstrip().startswith(('#', '!')):
                    values[match[1]] = match[2]
        else:
            try:
                values = {key: value for document in yaml.safe_load_all(text) if isinstance(document, dict)
                          for key, value in _flatten(document)}
            except yaml.YAMLError:
                continue
        # application.properties가 기본 프로필이다. 프로필별 파일은 기본값이 없을 때만 본다.
        found.append((path, values))
    found.sort(key=lambda item: item[0].name not in {'application.properties', 'application.yml', 'application.yaml'})
    return found


def _add(found: dict, kind: str, source: str, *, host, port=None, user=None, password=None, database=None):
    if kind in found or str(host).strip().lower() not in LOCAL_HOSTS:
        return
    try:
        port = int(port) if port not in (None, '') else DEFAULT_PORTS[kind]
    except ValueError:
        return
    if port != DEFAULT_PORTS[kind]:
        # 기본 포트가 아니면 앱 쪽 포워더와 DB 포트를 따로 맞춰야 한다. 지금은 재현하지 않는다.
        return
    if kind in {'mysql', 'postgres'} and (user is None or password is None or not database):
        return
    found[kind] = {'type': kind, 'port': port, 'user': user, 'password': password, 'database': database,
                   'source': source}


def _from_spring(root: Path, found: dict):
    for path, values in _spring_properties(root):
        source = path.relative_to(root).as_posix()
        url = _resolved(values.get('spring.datasource.url'))
        match = JDBC.search(url or '')
        if match:
            kind = 'postgres' if match[1] == 'postgresql' else 'mysql'
            _add(found, kind, source, host=match[2], port=match[3], database=match[4],
                 user=_resolved(values.get('spring.datasource.username')),
                 password=_resolved(values.get('spring.datasource.password', '')))
        mongo = MONGO_URI.search(_resolved(values.get('spring.data.mongodb.uri')) or '')
        if mongo:
            _add(found, 'mongo', source, host=mongo[3], port=mongo[4], database=mongo[5])
        elif values.get('spring.data.mongodb.host'):
            _add(found, 'mongo', source, host=_resolved(values['spring.data.mongodb.host']),
                 port=_resolved(values.get('spring.data.mongodb.port')))
        for prefix in ('spring.data.redis', 'spring.redis'):
            if values.get(prefix + '.host'):
                _add(found, 'redis', source, host=_resolved(values[prefix + '.host']),
                     port=_resolved(values.get(prefix + '.port')), password=_resolved(values.get(prefix + '.password')))


def _node_kind(root: Path) -> str | None:
    try:
        package = json.loads((root / 'package.json').read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    names = set()
    for key in ('dependencies', 'devDependencies'):
        if isinstance(package.get(key), dict):
            names |= set(package[key])
    if names & {'mysql', 'mysql2'}:
        return 'mysql'
    if 'pg' in names:
        return 'postgres'
    return None


def _from_json_and_scripts(root: Path, found: dict):
    kind = _node_kind(root)
    for path in _walk(root, ('*.json', 'config/*.json', '**/config.json', '**/*.js', '**/*.ts'), depth=3):
        if path.name in {'package.json', 'package-lock.json', 'tsconfig.json'}:
            continue
        source = path.relative_to(root).as_posix()
        # node-mysql-registration-login-api의 config.json은 BOM으로 시작한다.
        text = path.read_text(encoding='utf-8-sig', errors='replace')
        for mongo in MONGO_URI.finditer(text):
            _add(found, 'mongo', source, host=mongo[3], port=mongo[4], database=mongo[5])
        for redis in REDIS_URI.finditer(text):
            _add(found, 'redis', source, host=redis[2], port=redis[3], password=redis[1] or None)
        if kind and path.suffix == '.json':
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            stack = [data]
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    if isinstance(item.get('host'), str) and isinstance(item.get('database'), str):
                        _add(found, kind, source, host=item['host'], port=item.get('port'),
                             user=item.get('user', item.get('username')), password=item.get('password'),
                             database=item['database'])
                    stack.extend(item.values())
                elif isinstance(item, list):
                    stack.extend(item)


def _from_ini(root: Path, found: dict):
    for path in _walk(root, ('*.ini', 'conf/*.ini', 'config/*.ini'), depth=2):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(path.read_text(encoding='utf-8', errors='replace'))
        except configparser.Error:
            continue
        source = path.relative_to(root).as_posix()
        for name in parser.sections():
            section = {key.lower(): value.strip() for key, value in parser.items(name)}
            host, _, port = section.get('host', '').partition(':')
            kind = section.get('type', '').lower()
            if kind in {'mysql', 'postgres', 'postgresql'}:
                _add(found, 'postgres' if kind.startswith('postgres') else 'mysql', source, host=host, port=port,
                     user=section.get('user'), password=section.get('password', ''), database=section.get('name'))
            elif name.lower() == 'redis' and host:
                _add(found, 'redis', source, host=host, port=port, password=section.get('password') or None)


def detect_local_databases(root: Path) -> dict[str, dict]:
    """레포 설정에서 로컬 DB를 찾는다. 키는 종류(mysql, postgres, mongo, redis)다."""
    found: dict[str, dict] = {}
    for detector in (_from_spring, _from_json_and_scripts, _from_ini):
        detector(root, found)
    return found


def container_environment(database: dict) -> dict[str, str]:
    """레포에 적힌 계정 그대로 DB를 초기화하는 환경변수."""
    kind, user, password, name = database['type'], database.get('user'), database.get('password'), database.get('database')
    if kind == 'mysql':
        env = {'MYSQL_DATABASE': name}
        if user == 'root':
            env.update({'MYSQL_ROOT_PASSWORD': password} if password else {'MYSQL_ALLOW_EMPTY_PASSWORD': 'yes'})
        else:
            env.update(MYSQL_USER=user, MYSQL_PASSWORD=password, MYSQL_RANDOM_ROOT_PASSWORD='yes')
        return env
    if kind == 'postgres':
        env = {'POSTGRES_USER': user, 'POSTGRES_DB': name}
        env.update({'POSTGRES_PASSWORD': password} if password else {'POSTGRES_HOST_AUTH_METHOD': 'trust'})
        return env
    return {}


def container_arguments(database: dict) -> list[str]:
    if database['type'] == 'mysql':
        # 예제 레포는 `useSSL=false`로 붙는다. MySQL 8의 caching_sha2_password는 TLS 없이
        # 공개키를 받지 못해 `Public Key Retrieval is not allowed`로 끊기고, Node의 `mysql`
        # 패키지는 이 방식을 아예 모른다. 개발 환경에서 흔한 설정에 맞춘다.
        return ['--default-authentication-plugin=mysql_native_password']
    if database['type'] == 'redis' and database.get('password'):
        return ['redis-server', '--requirepass', database['password']]
    return []


def readiness_command(database: dict) -> list[str]:
    """초기화용 임시 서버는 TCP를 열지 않으므로 127.0.0.1로 물으면 진짜 서버만 응답한다."""
    return {'mysql': ['mysqladmin', 'ping', '-h', '127.0.0.1', '--silent'],
            'postgres': ['pg_isready', '-h', '127.0.0.1', '-q'],
            'mongo': ['mongosh', '--quiet', '--host', '127.0.0.1', '--eval', 'db.runCommand({ping: 1}).ok'],
            'redis': ['redis-cli', '-h', '127.0.0.1', 'ping']}[database['type']]


# 앱 Pod 안에서 localhost:<포트>로 오는 연결을 DB Service로 넘긴다. DB를 사이드카로 넣으면
# 앱 Pod 삭제 실험이 DB까지 지워 실험의 의미가 바뀌므로, DB는 따로 두고 연결만 넘긴다.
# 이미지는 부하·관측 Pod가 이미 쓰는 python:3.12-alpine이라 새로 받을 것이 없다.
FORWARDER = '''
import asyncio, socket, sys
async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data); await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
def handler(host, port):
    async def handle(reader, writer):
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
        except OSError:
            writer.close(); return
        await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
    return handle
async def main():
    servers = []
    for spec in sys.argv[1:]:
        local, host, port = spec.split(':')
        for address in ('127.0.0.1', '::1'):
            try:
                servers.append(await asyncio.start_server(handler(host, int(port)), address, int(local)))
            except OSError:
                pass
    await asyncio.gather(*(server.serve_forever() for server in servers))
asyncio.run(main())
'''
