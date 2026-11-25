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
PGVECTOR_IMAGE = 'pgvector/pgvector:0.8.7-pg17'
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


def _walk(root: Path, patterns: tuple[str, ...], depth: int = 3, skip: set[str] = SKIPPED_DIRECTORIES):
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            relative = path.relative_to(root)
            if (path.is_file() and len(relative.parts) <= depth + 1 and path.stat().st_size < 256_000
                    and not skip & set(relative.parts[:-1])):
                yield path


def _resource_patterns(scope: str) -> tuple[str, ...]:
    """src/<scope>/resources 를 레포 루트와 모듈 한 겹 아래에서 찾는 glob.

    모듈 한 겹을 더 보는 이유는 멀티모듈 Gradle/Maven 레포가 설정을 전부
    '<모듈>/src/main/resources' 에 두기 때문이다. 루트만 보면 그런 레포에서는
    DB 설정을 하나도 못 찾고, 의존성 없이 테스트를 돌려 커넥션 거부로 깨진다.
    """
    return tuple(f'{prefix}src/{scope}/resources/application*.{suffix}'
                 for prefix in ('', '*/')
                 for suffix in ('properties', 'yml', 'yaml'))


def _flatten(data, prefix=''):
    if isinstance(data, dict):
        for key, value in data.items():
            yield from _flatten(value, f'{prefix}{key}.')
    else:
        yield prefix.rstrip('.'), data


def _spring_properties(root: Path, *, include_tests: bool = False) -> list[tuple[Path, dict]]:
    found = []
    # 검사 단계에서는 테스트 설정을 먼저 본다. 우리가 실제로 돌리는 것이 테스트라
    # 그쪽에 적힌 주소와 계정이 맞고, main 은 운영 설정이라 다를 수 있다.
    # _add 가 먼저 찾은 것을 쓰므로 순서가 곧 우선순위다.
    #
    # 'test' 는 SKIPPED_DIRECTORIES 에 들어 있다. 픽스처나 샘플을 긁지 않으려고 둔
    # 것인데, 여기서는 우리가 경로를 명시해서 찾는 것이라 그 한 칸만 비켜준다.
    # node_modules 나 build 는 그대로 걸러진다.
    scopes = []
    if include_tests:
        scopes.append((_resource_patterns('test'), SKIPPED_DIRECTORIES - {'test', 'tests'}))
    scopes.append((_resource_patterns('main'), SKIPPED_DIRECTORIES))
    paths = [path for patterns, skip in scopes for path in _walk(root, patterns, depth=4, skip=skip)]

    for path in paths:
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
        found.append((path, values))
    defaults = [item for item in found if item[0].stem == 'application']
    if not defaults:
        # 기본 파일이 없으면 어느 프로필로 띄울지 모르므로 프로필별 파일을 모두 본다.
        return found
    # 기본 파일이 있으면 Spring은 그 파일과 활성 프로필의 파일만 읽는다. 다른 프로필의 DB는 앱이 붙지
    # 않는데도 띄우면 노드 CPU만 차지한다(spring-petclinic: 기본 H2인데 MySQL·Postgres 둘 다 떴다).
    active = {name.strip() for _, values in defaults
              for name in str(_resolved(values.get('spring.profiles.active')) or '').split(',') if name.strip()}
    profiles = [item for item in found if item[0].stem.startswith('application-')
                and item[0].stem[len('application-'):] in active]
    return defaults + profiles


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


def _from_spring(root: Path, found: dict, *, include_tests: bool = False):
    for path, values in _spring_properties(root, include_tests=include_tests):
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


def detect_local_databases(root: Path, *, include_tests: bool = False) -> dict[str, dict]:
    """레포 설정에서 로컬 DB를 찾는다. 키는 종류(mysql, postgres, mongo, redis)다.

    include_tests 는 검사(build/test) 단계에서만 켠다. 테스트가 쓰는 DB 주소는
    src/test/resources 에 적혀 있고 main 의 운영 설정과 다를 수 있는데, 우리가
    그 단계에서 실제로 돌리는 것은 테스트이기 때문이다.

    배포 단계는 끄고 쓴다. 거기서는 실제 앱이 뜨므로 main 설정이 맞고, 테스트
    계정으로 DB를 올리면 앱이 붙지 못한다.
    """
    found: dict[str, dict] = {}
    _from_spring(root, found, include_tests=include_tests)
    for detector in (_from_json_and_scripts, _from_ini):
        detector(root, found)
    return found


# 설정 값 안에 박힌 로컬 HTTP 주소. Ollama(11434)처럼 DB 가 아닌 의존성을 잡는다.
LOCAL_HTTP = re.compile(r'https?://(localhost|127\.0\.0\.1):(\d{2,5})')
MAX_DECLARED_ENDPOINTS = 24


def declared_endpoints(root: Path, *, include_tests: bool = False) -> list[dict]:
    """레포 설정이 기대하는 로컬 엔드포인트를 **종류별로 줄이지 않고** 전부 모은다.

    detect_local_databases 는 띄울 DB 를 고르는 함수라 종류마다 하나만 돌려준다.
    멀티모듈 레포는 모듈마다 다른 데이터베이스를 쓰는데, 그러면 나머지는 조용히 사라지고
    "왜 테스트가 깨졌는지" 를 설명할 근거도 같이 사라진다. 여기서는 설명이 목적이므로
    전부 남긴다.

    레포 로그가 아니라 레포 **설정**을 읽는다. 로그는 검사 대상이 마음대로 찍을 수 있어
    면제를 받아내는 통로가 되지만, 설정은 우리가 파싱한 사실이고 우리가 무엇을 띄웠는지도
    우리가 안다. 둘을 맞춰보는 것은 레포가 조작할 수 없다.
    """
    endpoints: list[dict] = []
    seen: set[tuple] = set()

    def record(kind, host, port, source, database=None):
        key = (kind, str(host), int(port), database)
        if key in seen or len(endpoints) >= MAX_DECLARED_ENDPOINTS:
            return
        seen.add(key)
        endpoints.append({'kind': kind, 'host': str(host), 'port': int(port),
                          'database': database, 'source': source})

    for path, values in _spring_properties(root, include_tests=include_tests):
        source = path.relative_to(root).as_posix()
        match = JDBC.search(_resolved(values.get('spring.datasource.url')) or '')
        if match and str(match[2]).strip().lower() in LOCAL_HOSTS:
            kind = 'postgres' if match[1] == 'postgresql' else 'mysql'
            record(kind, match[2], match[3] or DEFAULT_PORTS[kind], source, database=match[4])
        for value in values.values():
            # 자리표시자가 기본값을 갖지 않으면 주소를 모르는 것이므로 세지 않는다.
            for host, port in LOCAL_HTTP.findall(_resolved(value) or ''):
                record('http', host, port, source)
    return endpoints


def unmet_endpoints(declared: list[dict], provided: dict) -> dict[str, list[dict]]:
    """선언됐는데 우리가 제공하지 않은 것을 확신 수준으로 나눈다.

    databases 는 확신이 높다. 우리가 파싱해서 뜻을 알고, 띄울 수도 있었는데 안 띄운
    것이다. 같은 종류라도 데이터베이스 이름이 다르면 못 붙는다. 한 요청에 postgres 를
    하나만 띄우므로 모듈마다 이름이 다른 레포는 첫 번째만 연결된다.

    other 는 참고용이다. 설정에 적힌 로컬 HTTP 주소인데, 그 테스트가 실제로 그 주소를
    부르는지는 모른다. 실측 레포에서 형제 서비스 주소 네 개가 미충족이었지만 해당
    모듈은 그대로 통과했다. 이걸 실패 원인으로 보고하면 없는 인과를 만든다.

    그래서 판단(테스트 게이트를 넘길지)은 databases 로만 한다. other 는 리포트에
    맥락으로만 싣는다.
    """
    databases, other = [], []
    for endpoint in declared:
        supplied = provided.get(endpoint['kind'])
        if supplied and supplied.get('port') == endpoint['port'] and (
                endpoint['database'] is None or supplied.get('database') == endpoint['database']):
            continue
        (databases if endpoint['kind'] in DEFAULT_PORTS else other).append(endpoint)
    return {'databases': databases, 'other': other}


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
