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
from local_databases import container_arguments, container_environment, detect_local_databases
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
