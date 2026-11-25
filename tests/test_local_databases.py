import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from execution_plan import render_plan, resolve_plan
from local_databases import (blank_configuration, container_arguments, container_environment,
                             declared_endpoints, detect_local_databases, readiness_command, unmet_endpoints)
import repository_checks
from repository_checks import CheckFailure, verify_repository


class LocalDatabaseDetectionTests(unittest.TestCase):
    """실제 레포 배치에서 DB가 없어 판정 불가였던 10건의 설정 모양 그대로다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text, encoding='utf-8'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)

    def test_spring_properties_mysql_uses_the_repository_account(self):
        # bezkoder/spring-boot-spring-security-jwt-authentication
        self.write('src/main/resources/application.properties',
            'spring.datasource.url= jdbc:mysql://localhost:3306/testdb_spring?useSSL=false\n'
            'spring.datasource.username= root\nspring.datasource.password= 123456\n')
        mysql = detect_local_databases(self.root)['mysql']
        self.assertEqual((mysql['user'], mysql['password'], mysql['database']), ('root', '123456', 'testdb_spring'))
        self.assertEqual(container_environment(mysql), {'MYSQL_DATABASE': 'testdb_spring', 'MYSQL_ROOT_PASSWORD': '123456'})
        self.assertIn('--default-authentication-plugin=mysql_native_password', container_arguments(mysql))

    def test_spring_yaml_postgres_and_placeholder_defaults(self):
        # ali-bouali/spring-boot-3-jwt-security
        self.write('src/main/resources/application.yml', 'spring:\n  datasource:\n'
            '    url: jdbc:postgresql://localhost:5432/jwt_security\n    username: ${DB_USER:username}\n    password: password\n')
        postgres = detect_local_databases(self.root)['postgres']
        self.assertEqual(container_environment(postgres),
            {'POSTGRES_USER': 'username', 'POSTGRES_DB': 'jwt_security', 'POSTGRES_PASSWORD': 'password'})

    def test_multi_module_test_resources_are_detected_for_the_check_step(self):
        """prgrms-be-adv-devcourse/beadv7_7_GrafanaOn_BE 실측. 설정이 모듈 아래 test 에 있다.

        이 배치를 못 읽어서 DB 없이 테스트를 돌렸고, @SpringBootTest 가 커넥션 거부로
        깨진 것을 test_failure 로 보고했다. 레포 결함이 아니라 우리가 환경을 못 만든 것이다.
        """
        self.write('commerce-service/src/test/resources/application.yaml',
            'spring:\n  datasource:\n'
            '    url: ${DB_URL:jdbc:postgresql://localhost:5432/dear_test}\n'
            '    username: ${DB_USERNAME:root}\n    password: ${DB_PASSWORD:root}\n')

        postgres = detect_local_databases(self.root, include_tests=True)['postgres']
        self.assertEqual((postgres['user'], postgres['password'], postgres['database']),
                         ('root', 'root', 'dear_test'))
        self.assertEqual(postgres['source'], 'commerce-service/src/test/resources/application.yaml')

    def test_deploy_step_keeps_reading_main_not_test(self):
        """배포는 실제 앱이 뜨므로 main 설정이어야 한다. 테스트 계정으로 올리면 앱이 못 붙는다."""
        self.write('commerce-service/src/main/resources/application.yaml',
            'spring:\n  datasource:\n    url: jdbc:postgresql://localhost:5432/dear\n'
            '    username: app\n    password: secret\n')
        self.write('commerce-service/src/test/resources/application.yaml',
            'spring:\n  datasource:\n    url: jdbc:postgresql://localhost:5432/dear_test\n'
            '    username: root\n    password: root\n')

        # 기본값(배포 단계)은 main 만 본다.
        self.assertEqual(detect_local_databases(self.root)['postgres']['database'], 'dear')
        # 검사 단계는 test 가 이긴다. 둘 다 있을 때 순서가 곧 우선순위다.
        self.assertEqual(detect_local_databases(self.root, include_tests=True)['postgres']['database'], 'dear_test')

    def test_test_directories_outside_resources_are_still_skipped(self):
        """'test' 를 건너뛰는 규칙을 통째로 푼 것이 아니다. 지정한 경로만 비켜준다."""
        self.write('test/fixtures/src/main/resources/application.yaml',
            'spring:\n  datasource:\n    url: jdbc:postgresql://localhost:5432/fixture\n'
            '    username: u\n    password: p\n')
        self.assertEqual(detect_local_databases(self.root, include_tests=True), {})

    def test_inactive_profile_databases_are_not_started(self):
        # spring-projects/spring-petclinic: H2 by default, MySQL/Postgres only under their profiles.
        # Starting both took 1000m of the 4-core node and blocked measuring 2 replicas (2026-11-01).
        self.write('src/main/resources/application.properties', 'database=h2\nspring.jpa.open-in-view=false\n')
        self.write('src/main/resources/application-mysql.properties',
            'database=mysql\nspring.datasource.url=${MYSQL_URL:jdbc:mysql://localhost/petclinic}\n'
            'spring.datasource.username=${MYSQL_USER:petclinic}\nspring.datasource.password=${MYSQL_PASS:petclinic}\n')
        self.write('src/main/resources/application-postgres.properties',
            'spring.datasource.url=${POSTGRES_URL:jdbc:postgresql://localhost/petclinic}\n'
            'spring.datasource.username=${POSTGRES_USER:petclinic}\nspring.datasource.password=${POSTGRES_PASS:petclinic}\n')
        self.assertEqual(detect_local_databases(self.root), {})

    def test_active_profile_and_profile_only_repositories_still_detect(self):
        self.write('src/main/resources/application.yml', 'spring:\n  profiles:\n    active: dev\n')
        self.write('src/main/resources/application-dev.yml', 'spring:\n  datasource:\n'
            '    url: jdbc:postgresql://localhost:5432/app\n    username: app\n    password: secret\n')
        self.write('src/main/resources/application-prod.yml', 'spring:\n  datasource:\n'
            '    url: jdbc:mysql://localhost:3306/app\n    username: app\n    password: secret\n')
        self.assertEqual(set(detect_local_databases(self.root)), {'postgres'})
        other = Path(tempfile.mkdtemp()); self.addCleanup(lambda: __import__('shutil').rmtree(other))
        (other / 'src/main/resources').mkdir(parents=True)
        (other / 'src/main/resources/application-local.properties').write_text(
            'spring.datasource.url=jdbc:mysql://localhost:3306/app\nspring.datasource.username=root\n'
            'spring.datasource.password=root\n')
        self.assertEqual(set(detect_local_databases(other)), {'mysql'})

    def test_values_are_never_invented(self):
        self.write('src/main/resources/application.properties',
            'spring.datasource.url=jdbc:mysql://localhost:3306/app\nspring.datasource.username=${DB_USER}\n')
        self.assertEqual(detect_local_databases(self.root), {})
        self.write('src/main/resources/application.properties',
            'spring.datasource.url=jdbc:mysql://db.example.com:3306/app\n'
            'spring.datasource.username=root\nspring.datasource.password=x\n')
        self.assertEqual(detect_local_databases(self.root), {}, 'remote databases are not ours to reproduce')

    def test_node_json_config_with_bom_and_mongo_uri(self):
        # cornflourblue/node-mysql-registration-login-api, node-mongo-registration-login-api
        self.write('package.json', json.dumps({'dependencies': {'mysql2': '^2'}}))
        self.write('config.json', json.dumps({'database': {'host': 'localhost', 'port': 3306, 'user': 'root',
            'password': '', 'database': 'node-mysql'}}), encoding='utf-8-sig')
        self.write('_helpers/db.js', "mongoose.connect(process.env.MONGODB_URI || 'mongodb://localhost/node-mongo');")
        self.write('node_modules/x/config.js', "'mongodb://localhost/ignored'")
        found = detect_local_databases(self.root)
        self.assertEqual(container_environment(found['mysql']),
            {'MYSQL_DATABASE': 'node-mysql', 'MYSQL_ALLOW_EMPTY_PASSWORD': 'yes'})
        self.assertEqual(found['mongo']['database'], 'node-mongo')
        self.assertEqual(found['mongo']['source'], '_helpers/db.js')

    def test_go_ini_database_and_redis(self):
        # eddycjy/go-gin-example conf/app.ini
        self.write('conf/app.ini', '[database]\nType = mysql\nUser = root\nPassword = rootroot\n'
            'Host = 127.0.0.1:3306\nName = blog\n\n[redis]\nHost = 127.0.0.1:6379\nPassword =\n')
        found = detect_local_databases(self.root)
        self.assertEqual(sorted(found), ['mysql', 'redis'])
        self.assertEqual(found['mysql']['database'], 'blog')
        self.assertEqual(container_arguments(found['redis']), [])


class LocalDatabaseDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'src/main/resources').mkdir(parents=True)
        (self.root / 'pom.xml').write_text('<project><artifactId>spring-boot-starter-parent</artifactId></project>')
        (self.root / 'src/main/resources/application.properties').write_text(
            'spring.datasource.url=jdbc:postgresql://localhost:5432/testdb\n'
            'spring.datasource.username=postgres\nspring.datasource.password=123\n')

    def test_detected_database_is_deployed_with_the_repository_account_and_a_localhost_forwarder(self):
        plan = resolve_plan(self.root)
        self.assertEqual(list(plan['dependencies']), ['local-postgres'])
        manifest, target = render_plan(plan, 'codereferee-test', 'codereferee/test:abc')
        objects = list(yaml.safe_load_all(manifest))
        app = next(o for o in objects if o['kind'] == 'Deployment' and o['metadata']['name'] == 'repository-api')
        containers = app['spec']['template']['spec']['containers']
        self.assertEqual([c['name'] for c in containers], ['api', 'local-db-forward'])
        self.assertEqual(containers[1]['command'][-1], '5432:local-postgres:5432')
        database = next(o for o in objects if o['kind'] == 'Deployment' and o['metadata']['name'] == 'local-postgres')
        env = {e['name']: e['value'] for e in database['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env, {'POSTGRES_USER': 'postgres', 'POSTGRES_DB': 'testdb', 'POSTGRES_PASSWORD': '123'})
        # 앱 Pod 삭제 실험이 DB까지 지우지 않도록 DB는 따로 둔다.
        self.assertEqual(target['deployment'], 'repository-api')

    def test_declared_configuration_is_not_overridden(self):
        (self.root / '.codereferee').mkdir()
        (self.root / '.codereferee/validation.yaml').write_text('version: 1\nservice: {port: 8080}\n')
        self.assertEqual(resolve_plan(self.root)['dependencies'], {})


class NonDefaultDatabasePortTests(unittest.TestCase):
    """phdcoco/2026-team02-SCHIM-server 실측(2026-11-25). postgres 를 5433 에 적어 둔 레포다.

    로컬에 이미 postgres 가 있는 개발 환경에서 흔한 모양인데, 기본 포트가 아니라고 DB 를
    아예 띄우지 않아서 테스트는 Hibernate 가 dialect 를 못 정해 깨지고, 배포는 앱 Pod 가
    CrashLoopBackOff 로 올라오지 못했다. 우리가 환경을 안 만든 것이 원인이다.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'src/main/resources').mkdir(parents=True)
        (self.root / 'src/main/resources/application.yaml').write_text(
            'spring:\n  datasource:\n'
            '    url: ${SPRING_DATASOURCE_URL:jdbc:postgresql://localhost:5433/schim}\n'
            '    username: ${SPRING_DATASOURCE_USERNAME:schim}\n'
            '    password: ${SPRING_DATASOURCE_PASSWORD:schim}\n')
        (self.root / 'Dockerfile').write_text(
            'FROM eclipse-temurin:21-jre\nWORKDIR /app\nCOPY app.jar app.jar\n'
            'EXPOSE 8080\nENTRYPOINT ["java", "-jar", "app.jar"]\n')

    def test_the_declared_port_reaches_the_server_and_the_readiness_check(self):
        postgres = detect_local_databases(self.root)['postgres']
        self.assertEqual(postgres['port'], 5433)
        self.assertEqual((postgres['user'], postgres['password'], postgres['database']),
                         ('schim', 'schim', 'schim'))
        self.assertEqual(container_arguments(postgres), ['-p', '5433'])
        self.assertEqual(readiness_command(postgres), ['pg_isready', '-h', '127.0.0.1', '-p', '5433', '-q'])
        # 계정은 포트와 무관하게 레포에 적힌 그대로다.
        self.assertEqual(container_environment(postgres),
            {'POSTGRES_USER': 'schim', 'POSTGRES_DB': 'schim', 'POSTGRES_PASSWORD': 'schim'})

    def test_the_default_port_keeps_the_previous_arguments(self):
        (self.root / 'src/main/resources/application.yaml').write_text(
            'spring:\n  datasource:\n    url: jdbc:postgresql://localhost:5432/schim\n'
            '    username: schim\n    password: schim\n')
        postgres = detect_local_databases(self.root)['postgres']
        self.assertEqual(container_arguments(postgres), [])
        self.assertEqual(readiness_command(postgres), ['pg_isready', '-h', '127.0.0.1', '-p', '5432', '-q'])

    def test_forwarder_service_and_probe_all_use_the_declared_port(self):
        plan = resolve_plan(self.root)
        manifest, _ = render_plan(plan, 'codereferee-test', 'codereferee/test:abc')
        objects = list(yaml.safe_load_all(manifest))
        app = next(o for o in objects if o['kind'] == 'Deployment' and o['metadata']['name'] == 'repository-api')
        forwarder = next(c for c in app['spec']['template']['spec']['containers'] if c['name'] == 'local-db-forward')
        # 앱은 localhost:5433 을 본다. 그 포트에서 듣지 않으면 포워더가 있어도 닿지 않는다.
        self.assertEqual(forwarder['command'][-1], '5433:local-postgres:5433')
        database = next(o for o in objects if o['kind'] == 'Deployment' and o['metadata']['name'] == 'local-postgres')
        container = database['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['args'], ['-p', '5433'])
        self.assertEqual(container['ports'], [{'containerPort': 5433}])
        self.assertEqual(container['readinessProbe']['tcpSocket'], {'port': 5433})
        service = next(o for o in objects if o['kind'] == 'Service' and o['metadata']['name'] == 'local-postgres')
        self.assertEqual(service['spec']['ports'], [{'port': 5433, 'targetPort': 5433}])

    def test_blank_configuration_lists_only_the_keys_the_repository_left_empty(self):
        """R2 와 Kakao 키는 빈 기본값이고, 데이터소스 URL 은 실제 기본값이 있다."""
        (self.root / 'src/main/resources/application.yaml').write_text(
            'spring:\n  datasource:\n'
            '    url: ${SPRING_DATASOURCE_URL:jdbc:postgresql://localhost:5433/schim}\n'
            '    username: ${SPRING_DATASOURCE_USERNAME:schim}\n'
            '    password: ${SPRING_DATASOURCE_PASSWORD:schim}\n'
            'kakao:\n  rest-api-key: ${KAKAO_REST_API_KEY:}\n'
            'r2:\n  access-key: ${R2_ACCESS_KEY:}\n  secret-key: ${R2_SECRET_KEY:}\n'
            '  endpoint: ${R2_ENDPOINT:https://example.r2.cloudflarestorage.com}\n'
            '  bucket: ${R2_BUCKET:schim}\n')
        blank = blank_configuration(self.root)
        self.assertEqual([key['name'] for key in blank],
                         ['KAKAO_REST_API_KEY', 'R2_ACCESS_KEY', 'R2_SECRET_KEY'])
        self.assertEqual(blank[1]['property'], 'r2.access-key')
        self.assertEqual(blank[1]['source'], 'src/main/resources/application.yaml')

    def test_a_placeholder_without_any_default_also_counts(self):
        (self.root / 'src/main/resources/application.yaml').write_text(
            'kakao:\n  rest-api-key: ${KAKAO_REST_API_KEY}\n')
        self.assertEqual([key['name'] for key in blank_configuration(self.root)], ['KAKAO_REST_API_KEY'])

    def test_a_repository_that_fills_everything_in_reports_nothing(self):
        (self.root / 'src/main/resources/application.yaml').write_text(
            'spring:\n  datasource:\n    url: jdbc:postgresql://localhost:5432/schim\n'
            '    username: schim\n    password: schim\n')
        self.assertEqual(blank_configuration(self.root), [])

    def test_the_declared_endpoint_is_no_longer_reported_unmet(self):
        declared = declared_endpoints(self.root, include_tests=True)
        self.assertEqual([(e['kind'], e['port'], e['database']) for e in declared], [('postgres', 5433, 'schim')])
        provided = detect_local_databases(self.root, include_tests=True)
        self.assertEqual(unmet_endpoints(declared, provided), {'databases': [], 'other': []})


class LocalDatabaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repository'
        (self.root / 'src/main/resources').mkdir(parents=True)
        (self.root / 'src/test/java').mkdir(parents=True)
        (self.root / 'pom.xml').write_text('<project/>')
        (self.root / 'src/main/resources/application.properties').write_text(
            'spring.datasource.url=jdbc:mysql://localhost:3306/testdb\n'
            'spring.datasource.username=root\nspring.datasource.password=123456\n')

    @patch('repository_checks.time.sleep')
    @patch('repository_checks.subprocess.run')
    def test_tests_share_the_database_network_and_the_database_is_removed(self, run, sleep):
        run.return_value = subprocess.CompletedProcess([], 0, '', '')
        report = verify_repository(self.root, Path(self.temp.name) / 'cache', 'req')
        commands = [c.args[0] for c in run.call_args_list]
        started = next(c for c in commands if '--detach' in c)
        self.assertIn('MYSQL_ROOT_PASSWORD=123456', started)
        name = started[started.index('--name') + 1]
        test = next(c for c in commands if '--workdir' in c and 'test' in c)
        self.assertEqual(test[test.index('--network') + 1], 'container:' + name)
        self.assertIn(['docker', 'rm', '--force', '--volumes', name], commands)
        self.assertTrue(report['local_databases']['ready'])

    @patch('repository_checks.time.monotonic')
    @patch('repository_checks.time.sleep')
    @patch('repository_checks.subprocess.run')
    def test_database_that_never_starts_is_infrastructure_not_a_repository_failure(self, run, sleep, monotonic):
        ticks = iter(range(0, 100000, 50))
        monotonic.side_effect = lambda: next(ticks)
        run.side_effect = lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1 if command[:2] == ['docker', 'exec'] else 0, '', 'not ready')
        with self.assertRaises(CheckFailure) as failure:
            verify_repository(self.root, Path(self.temp.name) / 'cache', 'req')
        self.assertTrue(failure.exception.report['infrastructure_error'])
        self.assertEqual(failure.exception.report['failed_step'], 'prepare')
        self.assertTrue(failure.exception.report['check_database_cleanup']['removed'])


if __name__ == '__main__':
    unittest.main()


class UnmetDependencyCoverageTests(unittest.TestCase):
    """못 띄워준 의존성 때문에 깨진 테스트를 어떻게 다루는지 고정한다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repository'
        # 실측 레포(D:EAR)와 같은 모양. 모듈마다 데이터베이스 이름이 다르고,
        # 한 모듈은 로컬 HTTP 의존성(Ollama)까지 쓴다.
        for module, database, extra in (('commerce-service', 'dear_test', ''),
                                        ('recommendation-service', 'dear',
                                         '    ollama:\n      base-url: http://localhost:11434\n')):
            path = self.root / module / 'src/test/resources'
            path.mkdir(parents=True)
            (path / 'application.yaml').write_text(
                'spring:\n  datasource:\n'
                f'    url: jdbc:postgresql://localhost:5432/{database}\n'
                '    username: root\n    password: root\n' + extra, encoding='utf-8')
        (self.root / 'pom.xml').write_text('<project/>')

    def run_checks(self, test_exit_code):
        """테스트 단계만 실패시키고 나머지는 성공시킨다."""
        def fake(command, **kwargs):
            is_test = '--workdir' in command and any('test' in str(part) for part in command)
            return subprocess.CompletedProcess(command, test_exit_code if is_test else 0, '', '')
        with patch('repository_checks.time.sleep'), patch('repository_checks.subprocess.run', side_effect=fake):
            return verify_repository(self.root, Path(self.temp.name) / 'cache', 'req')

    def test_unmet_database_is_reported_with_its_source(self):
        report = self.run_checks(0)
        unmet = report['verification_coverage']['unmet_dependencies']
        # postgres 는 종류당 하나만 뜨므로 두 번째 데이터베이스는 못 붙는다.
        self.assertEqual([(e['kind'], e['database']) for e in unmet['databases']], [('postgres', 'dear')])
        self.assertEqual(unmet['databases'][0]['source'],
                         'recommendation-service/src/test/resources/application.yaml')
        # Ollama 는 확신이 낮은 축에 들어간다. 판단에는 쓰지 않는다.
        self.assertIn(11434, [e['port'] for e in unmet['other']])

    def test_test_failure_from_an_unmet_database_does_not_block_the_chaos_stage(self):
        # 멈추면 baseline 이 안 서고 카오스를 못 건다. 이 제품이 묻는 질문이 거기 있다.
        report = self.run_checks(1)
        self.assertEqual(report['outcome'], 'passed')
        self.assertEqual(report['test_execution'], 'blocked_by_unmet_dependencies')

    def test_continuing_never_claims_full_coverage(self):
        # 넘어가되 통과로 치지 않는다. 범위 제한이 최종 판정까지 따라가야 한다.
        coverage = self.run_checks(1)['verification_coverage']
        self.assertEqual(coverage['level'], 'partial')
        self.assertEqual(coverage['reason'], 'unmet_dependencies')

    def test_coverage_is_full_when_everything_was_provided(self):
        for path in self.root.glob('recommendation-service/src/test/resources/application.yaml'):
            path.write_text('spring:\n  datasource:\n'
                '    url: jdbc:postgresql://localhost:5432/dear_test\n'
                '    username: root\n    password: root\n', encoding='utf-8')
        report = self.run_checks(0)
        self.assertEqual(report['verification_coverage']['level'], 'full')
        self.assertEqual(report['verification_coverage']['unmet_dependencies']['databases'], [])

    def test_a_real_test_failure_still_blocks(self):
        """면제는 우리가 못 띄워준 것이 있을 때만이다. 그 구멍을 다시 뚫지 않는다.

        미충족 의존성이 없는데도 넘어가면 검증되지 않은 코드에 합격 도장을 주게 된다.
        이 레포가 exit 89 와 '테스트가 없으면 합격 아님' 으로 반복해서 막아온 모양이다.
        """
        for path in self.root.glob('*/src/test/resources/application.yaml'):
            path.write_text('spring:\n  datasource:\n'
                '    url: jdbc:postgresql://localhost:5432/dear_test\n'
                '    username: root\n    password: root\n', encoding='utf-8')
        with self.assertRaises(CheckFailure) as failure:
            self.run_checks(1)
        report = failure.exception.report
        self.assertEqual(report['outcome'], 'failed')
        self.assertEqual(report['failed_step'], 'test')
        self.assertEqual(report['test_execution'], 'failed')

    def test_infrastructure_failures_are_not_excused_as_unmet_dependencies(self):
        """exit 125 는 우리 쪽 도커 문제다. 의존성 핑계로 넘기면 원인이 묻힌다."""
        with self.assertRaises(CheckFailure) as failure:
            self.run_checks(125)
        self.assertTrue(failure.exception.report['infrastructure_error'])
