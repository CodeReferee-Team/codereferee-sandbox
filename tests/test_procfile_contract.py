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

    def test_shell_or_unproven_script_requires_configuration(self):
        for command in (r'gunicorn service:app -b 0.0.0.0:\$PORT',
                        'gunicorn service:app -b 0.0.0.0:$PORT && echo done',
                        'python serve.py --host 0.0.0.0 --port $PORT',
                        'gunicorn service:app -b 0.0.0.0:$PORT -b 127.0.0.1:9000'):
            with self.subTest(command=command), self.assertRaises(ConfigurationRequired):
                self.plan(command)


if __name__ == '__main__':
    unittest.main()
