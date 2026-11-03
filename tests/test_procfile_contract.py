import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from execution_plan import ConfigurationRequired, resolve_plan


class ProcfileContractTests(unittest.TestCase):
    def plan(self, command):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Procfile').write_text('web: ' + command + '\n', encoding='utf-8')
            (root / 'requirements.txt').write_text('gunicorn\nflask\n', encoding='utf-8')
            return resolve_plan(root)

    def test_port_prefix_is_not_the_port_variable(self):
        with self.assertRaises(ConfigurationRequired):
            self.plan('gunicorn autoapp:app -b 0.0.0.0:$PORT_SUFFIX')

    def test_missing_requirements_requires_explicit_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Procfile').write_text('web: uvicorn entrypoint:app --host 0.0.0.0 --port $PORT\n', encoding='utf-8')
            (root / 'entrypoint.py').write_text('value = 1\n', encoding='utf-8')
            with self.assertRaisesRegex(ConfigurationRequired, 'requires requirements.txt'):
                resolve_plan(root)

    def test_pyproject_only_does_not_claim_supported_dependency_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Procfile').write_text('web: gunicorn service:app -b 0.0.0.0:$PORT\n', encoding='utf-8')
            (root / 'pyproject.toml').write_text('[project]\nname = "example"\nversion = "0.1.0"\n', encoding='utf-8')
            with self.assertRaisesRegex(ConfigurationRequired, 'requires requirements.txt'):
                resolve_plan(root)

    def test_comment_does_not_declare_binding(self):
        with self.assertRaises(ConfigurationRequired):
            self.plan('gunicorn autoapp:app -b 0.0.0.0:5000 # $PORT')

    def test_port_assignment_cannot_override_generated_port(self):
        with self.assertRaises(ConfigurationRequired):
            self.plan('PORT=9000 gunicorn autoapp:app -b 0.0.0.0:$PORT')

    def test_port_in_other_option_does_not_declare_binding(self):
        with self.assertRaises(ConfigurationRequired):
            self.plan('gunicorn autoapp:app -b 0.0.0.0:5000 --name=$PORT')

    def test_loopback_bind_is_not_reachable_by_kubernetes(self):
        with self.assertRaises(ConfigurationRequired):
            self.plan('gunicorn autoapp:app -b 127.0.0.1:$PORT')

    def test_single_quotes_do_not_expand_port_in_shell(self):
        with self.assertRaises(ConfigurationRequired):
            self.plan("gunicorn autoapp:app -b '0.0.0.0:$PORT'")

    def test_multiple_web_commands_are_ambiguous(self):
        with self.assertRaises(ConfigurationRequired):
            self.plan('gunicorn autoapp:app -b 0.0.0.0:$PORT\nweb: uvicorn other:app --port $PORT --host 0.0.0.0')

    def test_valid_declared_python_runners(self):
        for command in ('gunicorn autoapp:app --bind=0.0.0.0:${PORT}',
                        'uvicorn main:app --host 0.0.0.0 --port "$PORT"',
                        'python -m uvicorn main:app --host 0.0.0.0 --port $PORT',
                        'flask --app service run --host=0.0.0.0 --port=$PORT'):
            with self.subTest(command=command):
                plan = self.plan(command)
                self.assertEqual(plan['port'], 8000)
                self.assertIn('RUN pip install --no-cache-dir -r requirements.txt', plan['generatedDockerfile'])

    def test_shell_or_unproven_script_requires_configuration(self):
        for command in (r'gunicorn service:app -b 0.0.0.0:\$PORT',
                        'gunicorn service:app -b 0.0.0.0:$PORT && echo done',
                        'python serve.py --host 0.0.0.0 --port $PORT',
                        'gunicorn service:app -b 0.0.0.0:$PORT -b 127.0.0.1:9000'):
            with self.subTest(command=command), self.assertRaises(ConfigurationRequired):
                self.plan(command)

    def test_incompatible_socket_and_process_modes_require_configuration(self):
        commands = (
            'uvicorn entrypoint:app --host 0.0.0.0 --port $PORT --uds /tmp/app.sock',
            'uvicorn entrypoint:app --host 0.0.0.0 --port $PORT --fd=3',
            'uvicorn entrypoint:app --host 0.0.0.0 --port $PORT --ssl-certfile cert.pem',
            'gunicorn entrypoint:app -b 0.0.0.0:$PORT --daemon',
            'gunicorn entrypoint:app -b 0.0.0.0:$PORT -D',
            'gunicorn entrypoint:app -b 0.0.0.0:$PORT --check-config',
            'daphne -b 0.0.0.0 -p $PORT -u/tmp/app.sock entrypoint:app',
            'waitress-serve --listen=0.0.0.0:$PORT --port=9000 entrypoint:app',
            'uvicorn entrypoint:app -- --host 0.0.0.0 --port $PORT',
        )
        for command in commands:
            with self.subTest(command=command), self.assertRaises(ConfigurationRequired):
                self.plan(command)

    def test_runner_paths_and_console_script_module_are_not_guessed(self):
        for command in ('.venv/bin/gunicorn entrypoint:app -b 0.0.0.0:$PORT',
                        '/usr/bin/uvicorn entrypoint:app --host 0.0.0.0 --port $PORT',
                        'python -m waitress-serve --host 0.0.0.0 --port $PORT entrypoint:app'):
            with self.subTest(command=command), self.assertRaises(ConfigurationRequired):
                self.plan(command)

    def test_shell_comment_and_environment_changes_are_not_misread(self):
        for command in ('uvicorn entrypoint:app --host 0.0.0.0 --port $PORT#note',
                        'uvicorn entrypoint:app --host 0.0.0.0 --port $PORT # note',
                        'PATH=/nowhere gunicorn entrypoint:app -b 0.0.0.0:$PORT',
                        'uvicorn entrypoint:app --host 0.0.0.0 --port $PORT $EXTRA',
                        'gunicorn entrypoint:app -b 0.0.0.0:$PORT > output.log'):
            with self.subTest(command=command), self.assertRaises(ConfigurationRequired):
                self.plan(command)

    def test_generated_port_cannot_be_overridden_by_yaml_environment(self):
        for value in ('8000', '9000'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'Procfile').write_text('web: uvicorn entrypoint:app --host 0.0.0.0 --port $PORT\n')
                (root / 'requirements.txt').write_text('uvicorn\n')
                (root / '.codereferee').mkdir()
                (root / '.codereferee/validation.yaml').write_text(
                    'version: 1\nservice:\n  port: 8000\n  env:\n    PORT: "' + value + '"\n')
                if value == '8000':
                    self.assertEqual(resolve_plan(root)['env']['PORT'], value)
                else:
                    with self.assertRaisesRegex(ConfigurationRequired, 'conflicts'):
                        resolve_plan(root)

    def test_explicit_dockerfile_keeps_priority_over_unsupported_procfile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Procfile').write_text('web: .venv/bin/gunicorn entrypoint:app -b 0.0.0.0:$PORT\n')
            (root / 'Dockerfile').write_text('FROM python:3.12-slim\nEXPOSE 8080\n')
            self.assertEqual(resolve_plan(root)['source'], 'dockerfile')
            (root / '.codereferee').mkdir()
            (root / '.codereferee/validation.yaml').write_text(
                'version: 1\nservice:\n  dockerfile: Dockerfile\n  port: 8080\n  env: {PORT: "9000"}\n')
            self.assertEqual(resolve_plan(root)['source'], 'repository_configuration')

    def test_other_supported_direct_runners_and_custom_port(self):
        for command in ('hypercorn entrypoint:app --bind 0.0.0.0:${PORT}',
                        'daphne -b 0.0.0.0 -p $PORT entrypoint:app',
                        'waitress-serve --listen=0.0.0.0:$PORT entrypoint:app',
                        'waitress-serve --host=0.0.0.0 --port=$PORT entrypoint:app'):
            with self.subTest(command=command):
                self.assertEqual(self.plan(command)['port'], 8000)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Procfile').write_text('web: uvicorn entrypoint:app --host 0.0.0.0 --port $PORT\n')
            (root / 'requirements.txt').write_text('uvicorn\n')
            (root / '.codereferee').mkdir()
            (root / '.codereferee/validation.yaml').write_text('version: 1\nservice: {port: 9000, env: {PORT: "9000"}}\n')
            self.assertIn('ENV PORT=9000', resolve_plan(root)['generatedDockerfile'])


if __name__ == '__main__':
    unittest.main()
