import json
from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from execution_plan import ConfigurationRequired, inside, java_release, node_major, normalize_resources, render_plan, resolve_plan


class ExecutionPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def test_explicit_and_checked_in_configuration_override_inference(self):
        self.write('.codereferee/validation.yaml', 'deploymentProfile: quickbyte-demo\n')
        self.assertEqual(resolve_plan(self.root)['profile'], 'quickbyte-demo')
        self.assertEqual(resolve_plan(self.root, 'custom')['profile'], 'custom')

    def test_dockerfile_single_exposed_port_is_inferred_without_profile(self):
        self.write('Dockerfile', 'FROM node:20\nEXPOSE 3000\n')
        plan = resolve_plan(self.root)
        self.assertEqual(plan['source'], 'dockerfile')
        self.assertEqual(plan['port'], 3000)
        manifest, target = render_plan(plan, 'codereferee-test', 'codereferee/test:123456789abc')
        self.assertEqual(target['deployment'], 'repository-api')
        self.assertIn('automountServiceAccountToken: false', manifest)

    def test_ambiguous_ports_and_library_require_configuration(self):
        with self.assertRaises(ConfigurationRequired):
            resolve_plan(self.root)
        self.write('Dockerfile', 'FROM nginx\nEXPOSE 80 443\n')
        with self.assertRaises(ConfigurationRequired):
            resolve_plan(self.root)

    def test_node_without_dockerfile_gets_generated_recipe(self):
        self.write('package.json', json.dumps({'scripts': {'start': 'node src/index.js'}}))
        plan = resolve_plan(self.root)
        self.assertEqual(plan['source'], 'stack_detection')
        self.assertIn('CMD ["npm","run","start"]', plan['generatedDockerfile'])

    def test_repository_paths_cannot_escape_clone(self):
        with self.assertRaises(ConfigurationRequired):
            inside(self.root, '../Dockerfile')
        with self.assertRaises(ConfigurationRequired):
            inside(self.root, '.git/config')
        with self.assertRaises(ConfigurationRequired):
            inside(self.root, self.root.as_posix())

    def test_compose_redis_network_is_reproduced_without_host_mounts(self):
        self.write('Dockerfile', 'FROM python:3.12\nEXPOSE 5000\n')
        self.write('compose.yaml', 'services:\n  web:\n    build: .\n    ports: ["8000:5000"]\n    environment: {REDIS_HOST: redis}\n  redis:\n    image: redis:7-alpine\n')
        plan = resolve_plan(self.root)
        self.assertEqual(plan['source'], 'compose')
        self.assertEqual(plan['port'], 5000)
        manifest, _ = render_plan(plan, 'codereferee-test', 'test')
        self.assertIn('redis:7-alpine', manifest)
        self.assertNotIn('hostPath', manifest)

    def test_multi_service_compose_does_not_silently_choose_a_target(self):
        self.write('compose.yaml', 'services:\n  a: {build: .}\n  b: {build: .}\n')
        with self.assertRaises(ConfigurationRequired):
            resolve_plan(self.root)

    def test_compose_without_build_target_falls_through_to_repository_detection(self):
        # DB만 띄우는 Compose(spring-petclinic 등)나 배포 이미지를 당겨 쓰는 Compose는
        # 앱을 어떻게 빌드하는지 말하지 않는다. 그런 파일 때문에 Dockerfile과 스택 탐색을 막지 않는다.
        self.write('compose.yaml', 'services:\n  mysql: {image: mysql:9}\n  postgres: {image: postgres:17}\n')
        self.write('pom.xml', '<project><groupId>org.springframework.boot</groupId></project>')
        plan = resolve_plan(self.root)
        self.assertEqual(plan['source'], 'stack_detection')
        self.assertEqual(plan['dependencies'], {})
        self.write('Dockerfile', 'FROM node:20\nEXPOSE 3001\n')
        self.write('compose.yaml', 'services:\n  app: {image: louislam/uptime-kuma:1, ports: ["3001:3001"]}\n')
        plan = resolve_plan(self.root)
        self.assertEqual((plan['source'], plan['port']), ('dockerfile', 3001))
        self.write('compose.yaml', 'services: {}\n')
        self.assertEqual(resolve_plan(self.root)['source'], 'dockerfile')

    def test_java_release_follows_declarations_outside_the_main_build_file(self):
        self.assertEqual(java_release(self.root), '17')
        self.write('pom.xml', '<project><properties><java.version>1.8</java.version></properties></project>')
        self.assertEqual(java_release(self.root), '17')
        # Gradle toolchain은 정확히 같은 JDK가 있어야 하므로 버전 카탈로그 값까지 본다.
        (self.root / 'pom.xml').unlink()
        self.write('build.gradle.kts', 'java { toolchain { languageVersion.set(JavaLanguageVersion.of(libs.versions.java.get())) } }')
        self.write('gradle/libs.versions.toml', '[versions]\njava = "21"\nspring = "3.2.0"\n')
        self.assertEqual(java_release(self.root), '21')
        self.write('build.gradle.kts', 'java { sourceCompatibility = JavaVersion.VERSION_25 }\nplugins { id("org.springframework.boot") }')
        (self.root / 'gradle/libs.versions.toml').unlink()
        self.write('gradlew', '')
        self.assertEqual(java_release(self.root), '25')
        self.assertIn('FROM eclipse-temurin:25-jdk AS builder', resolve_plan(self.root)['generatedDockerfile'])

    def test_generated_flask_command_works_with_flask_1(self):
        self.write('requirements.txt', 'Flask==1.1.2\n')
        self.write('app.py', 'from flask import Flask\napp = Flask(__name__)\n')
        recipe = resolve_plan(self.root)['generatedDockerfile']
        self.assertIn('ENV FLASK_APP=app\n', recipe)
        self.assertNotIn('--app', recipe)

    def test_node_image_follows_engines_and_nvmrc_but_never_drops_below_20(self):
        self.assertEqual(node_major(self.root, {}), '20')
        self.assertEqual(node_major(self.root, {'engines': {'node': '>=10'}}), '20')
        self.assertEqual(node_major(self.root, {'engines': {'node': '>=22.12.0'}}), '22')
        self.assertEqual(node_major(self.root, {'engines': {'node': '^22.22.2 || ^24.15.0 || >=26.0.0'}}), '22')
        self.write('.nvmrc', 'v24.16.0\n')
        self.assertEqual(node_major(self.root, {'engines': {'node': '>=22'}}), '24')
        self.write('package.json', json.dumps({'scripts': {'start': 'node index.js'}}))
        self.assertIn('FROM node:24-bookworm-slim', resolve_plan(self.root)['generatedDockerfile'])

    def test_yaml_postgres_password_is_generated_and_shared_with_app_only(self):
        self.write('Dockerfile', 'FROM python:3.12\n')
        self.write('.codereferee/validation.yaml', 'version: 1\nservice:\n  port: 8000\n  env:\n    DB_PASSWORD: "{{dependency.db.password}}"\ndependencies:\n  db: {type: postgres}\n')
        import yaml
        objects = list(yaml.safe_load_all(render_plan(resolve_plan(self.root), 'codereferee-test', 'test')[0]))
        passwords = []
        for obj in objects:
            if obj['kind'] == 'Deployment':
                for env in obj['spec']['template']['spec']['containers'][0].get('env', []):
                    if 'PASSWORD' in env['name']:
                        passwords.append(env['value'])
        self.assertEqual(len(passwords), 2)
        self.assertEqual(passwords[0], passwords[1])
        self.assertEqual(len(passwords[0]), 48)

    def test_host_environment_interpolation_is_not_loaded(self):
        self.write('Dockerfile', 'FROM node:20\n')
        self.write('.codereferee/validation.yaml', 'version: 1\nservice:\n  port: 3000\n  env: {TOKEN: "${SECRET}"}\n')
        with self.assertRaises(ConfigurationRequired):
            resolve_plan(self.root)

    def test_resource_requests_never_exceed_small_declared_limits(self):
        limits = normalize_resources({'cpu': '50m', 'memory': '32Mi'})
        self.assertEqual(limits['requests'], {'cpu': '50m', 'memory': '32Mi'})
        with self.assertRaises(ConfigurationRequired):
            normalize_resources({'memory': '8Gi'})

    def test_final_docker_stage_port_only_and_udp_requires_configuration(self):
        self.write('Dockerfile', 'FROM node:20 AS builder\nEXPOSE 9000\nFROM nginx\nEXPOSE 80/tcp\n')
        self.assertEqual(resolve_plan(self.root)['port'], 80)
        self.write('Dockerfile', 'FROM alpine\nEXPOSE 53/udp\n')
        with self.assertRaises(ConfigurationRequired):
            resolve_plan(self.root)

    def test_compose_conflicting_port_requires_configuration_before_build(self):
        self.write('Dockerfile', 'FROM python:3.12\nEXPOSE 5000\n')
        self.write('compose.yaml', 'services:\n  web: {build: ., ports: ["8000:8000"]}\n')
        with self.assertRaisesRegex(ConfigurationRequired, 'conflicts'):
            resolve_plan(self.root)

    def test_malformed_compose_returns_configuration_required(self):
        self.write('Dockerfile', 'FROM node:20\nEXPOSE 3000\n')
        for config in ('services: {web: {build: ., ports: [{published: 3000}]}}',
                       'services: {web: {build: ., ports: [3000], environment: [null]}}',
                       'services: {web: {build: ., ports: [3000]}, redis: null}',
                       'services: {web: {build: {dockerfile: absent}, ports: [3000]}}',
                       'services: ['):
            with self.subTest(config=config):
                self.write('compose.yaml', config)
                with self.assertRaises(ConfigurationRequired):
                    resolve_plan(self.root)

    def test_malformed_package_is_configuration_error_not_infrastructure(self):
        for package in ('{', '[]', '{"scripts": []}'):
            with self.subTest(package=package):
                self.write('package.json', package)
                with self.assertRaises(ConfigurationRequired):
                    resolve_plan(self.root)

    def test_mysql_reproduction_credentials_and_business_probe(self):
        self.write('Dockerfile', 'FROM eclipse-temurin:17\n')
        self.write('.codereferee/validation.yaml', 'version: 1\nservice:\n  port: 8080\n  env:\n    DB_PASSWORD: "{{dependency.db.password}}"\ndependencies:\n  db: {type: mysql, probePath: /categories}\n')
        import yaml
        objects = list(yaml.safe_load_all(render_plan(resolve_plan(self.root), 'codereferee-test', 'test')[0]))
        app = objects[1]['spec']['template']['spec']['containers'][0]
        database = objects[3]['spec']['template']['spec']['containers'][0]
        env = {value['name']: value['value'] for value in database['env']}
        self.assertEqual(app['env'][0]['value'], env['MYSQL_PASSWORD'])
        self.assertNotEqual(env['MYSQL_ROOT_PASSWORD'], env['MYSQL_PASSWORD'])
        self.assertEqual(database['image'], 'mysql:8.4')
        self.assertEqual(database['readinessProbe']['tcpSocket']['port'], 3306)

    def test_same_kind_business_dependencies_do_not_overwrite_target(self):
        self.write('Dockerfile', 'FROM nginx\n')
        self.write('.codereferee/validation.yaml', 'version: 1\nservice: {port: 8080}\ndependencies:\n  a: {type: postgres, probePath: /a}\n  b: {type: mysql, probePath: /b}\n')
        with self.assertRaises(ConfigurationRequired):
            resolve_plan(self.root)

    def test_declared_node_port_can_generate_dockerfile_without_a_profile(self):
        self.write('package.json', json.dumps({'scripts': {'start': 'node app.js'}}))
        self.write('.codereferee/validation.yaml', 'version: 1\nservice: {port: 5000}\n')
        plan = resolve_plan(self.root)
        self.assertEqual(plan['port'], 5000)
        self.assertIn('ENV PORT=5000', plan['generatedDockerfile'])

    def test_node_port_comes_from_the_entry_file_when_it_ignores_PORT(self):
        # node-role-based-authorization-api: `NODE_ENV === 'production' ? 80 : 4000`이라 3000을 보던 프로브가 실패했다.
        self.write('package.json', json.dumps({'scripts': {'start': 'nodemon ./server.js'}}))
        self.write('server.js', "const port = process.env.NODE_ENV === 'production' ? 80 : 4000;\napp.listen(port);\n")
        plan = resolve_plan(self.root)
        self.assertEqual(plan['port'], 4000)
        self.assertIn('EXPOSE 4000', plan['generatedDockerfile'])

    def test_node_port_read_only_in_production_is_not_followed(self):
        # node-mongo-registration-login-api: PORT는 NODE_ENV=production일 때만 읽는다.
        self.write('package.json', json.dumps({'scripts': {'start': 'node ./server.js'}}))
        self.write('server.js', "const port = process.env.NODE_ENV === 'production' ? (process.env.PORT || 80) : 4000;\n")
        self.assertEqual(resolve_plan(self.root)['port'], 4000)

    def test_node_port_stays_3000_when_the_app_reads_PORT_or_is_ambiguous(self):
        self.write('package.json', json.dumps({'main': 'index.js', 'scripts': {'start': 'node .'}}))
        self.write('index.js', 'app.listen(process.env.PORT || 5000);\n')
        self.assertEqual(resolve_plan(self.root)['port'], 3000)
        self.write('index.js', 'app.listen(5000);\nadmin.listen(5001);\n')
        self.assertEqual(resolve_plan(self.root)['port'], 3000)
        self.write('index.js', 'app.listen(5000);\n')
        self.assertEqual(resolve_plan(self.root)['port'], 5000)

    def test_python_recipe_can_build_native_requirements(self):
        self.write('app.py', 'app = Flask(__name__)\n')
        self.write('requirements.txt', 'multidict\n')
        recipe = resolve_plan(self.root)['generatedDockerfile']
        self.assertIn('build-essential pkg-config', recipe)

    def test_python_recipe_installs_poetry_non_package_dependencies(self):
        self.write('app.py', 'app = FastAPI()\n')
        self.write('pyproject.toml', '[tool.poetry]\npackage-mode = false\n[tool.poetry.dependencies]\n'
            'python = "^3.10"\nsqlmodel = "^0.0.16"\n[tool.poetry.group.dev.dependencies]\npytest = "*"\n')
        recipe = resolve_plan(self.root)['generatedDockerfile']
        self.assertIn("'sqlmodel>=0.0.16,<0.0.17'", recipe)
        self.assertNotIn('pytest', recipe)

    def test_maven_multimodule_without_a_root_application_needs_configuration(self):
        # spring-guides/tut-rest: 모듈 네 개가 각각 앱이다. 어느 것을 띄울지 고르지 않는다.
        self.write('pom.xml', '<project><parent><artifactId>spring-boot-starter-parent</artifactId></parent>'
            '<modules><module>rest</module><module>links</module></modules></project>')
        self.write('rest/src/main/java/App.java', 'class App {}')
        with self.assertRaisesRegex(ConfigurationRequired, 'multi-module'): resolve_plan(self.root)

    def test_replica_override_keeps_detection_and_local_databases(self):
        # 수정안 루프가 배포 파일 없는 레포의 복제본 수를 올릴 때 쓰는 최소 선언이다.
        self.write('pom.xml', '<project><artifactId>spring-boot-starter-parent</artifactId></project>')
        self.write('src/main/resources/application.properties', 'spring.datasource.url=jdbc:mysql://localhost:3306/app\n'
                   'spring.datasource.username=root\nspring.datasource.password=pw\n')
        detected = resolve_plan(self.root)
        self.write('.codereferee/validation.yaml', 'version: 1\nservice:\n  replicas: 2\n')
        plan = resolve_plan(self.root)
        self.assertEqual(plan['replicas'], 2)
        self.assertEqual((plan['source'], plan['port'], plan['generatedDockerfile'], plan['dependencies']),
                         (detected['source'], detected['port'], detected['generatedDockerfile'], detected['dependencies']))
        self.assertFalse(plan['healthPathDeclared'])
        self.write('.codereferee/validation.yaml', 'version: 1\nservice:\n  replicas: 9\n')
        with self.assertRaises(ConfigurationRequired): resolve_plan(self.root)

    def test_explicit_multimodule_jar_is_selected_not_guessed(self):
        self.write('modules/api/build/libs/application.jar', 'jar')
        self.write('modules/other/build/libs/other.jar', 'jar')
        self.write('.codereferee/validation.yaml', 'version: 1\nservice:\n  port: 8080\n  artifactPath: modules/api/build/libs/application.jar\n')
        recipe = resolve_plan(self.root)['generatedDockerfile']
        self.assertIn('modules/api/build/libs/application.jar', recipe)
        self.assertNotIn('other.jar', recipe)
        self.write('.codereferee/validation.yaml', 'version: 1\nservice: {port: 8080, artifactPath: absent.jar}\n')
        with self.assertRaises(ConfigurationRequired): resolve_plan(self.root)
