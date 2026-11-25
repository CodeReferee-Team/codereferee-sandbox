import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from execution_plan import ConfigurationRequired, POSTGIS_IMAGE, normalize_dependencies, render_plan, resolve_plan
from multi_service import resolve_services, render_services


class PostgresInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'Dockerfile').write_text('FROM python:3.12\nEXPOSE 8000\n')
        (self.root / 'schema.sql').write_text('CREATE TABLE marker (id integer);', encoding='utf-8')

    def config(self):
        return {'version': 1, 'service': {'port': 8000, 'env': {
            'DB_PASSWORD': '{{dependency.db.password}}'}}, 'dependencies': {'db': {
                'type': 'postgres', 'extensions': ['postgis', 'uuid-ossp'], 'initSql': ['schema.sql']}}}

    def plan(self, config=None):
        (self.root / '.codereferee').mkdir(exist_ok=True)
        (self.root / '.codereferee/validation.yaml').write_text(yaml.safe_dump(config or self.config()), encoding='utf-8')
        return resolve_plan(self.root)

    def test_spatial_database_initializes_extensions_then_repository_schema_in_request_namespace(self):
        docs = list(yaml.safe_load_all(render_plan(self.plan(), 'request-spatial', 'test/app:1')[0]))
        config = next(d for d in docs if d['kind'] == 'ConfigMap')
        self.assertEqual(config['metadata'], {'name': 'db-init-sql', 'namespace': 'request-spatial'})
        self.assertEqual(sorted(config['data']), ['00-extensions.sql', '01-repository.sql'])
        self.assertIn('CREATE EXTENSION IF NOT EXISTS "postgis";', config['data']['00-extensions.sql'])
        self.assertEqual(config['data']['01-repository.sql'], 'CREATE TABLE marker (id integer);')
        pod = next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'db')['spec']['template']['spec']
        self.assertEqual(pod['containers'][0]['image'], POSTGIS_IMAGE)
        self.assertEqual(pod['volumes'], [{'name': 'init-sql', 'configMap': {'name': 'db-init-sql'}}])
        self.assertTrue(pod['containers'][0]['volumeMounts'][0]['readOnly'])
        self.assertNotIn('hostPath', yaml.safe_dump(docs))

    def test_regular_postgres_and_vector_keep_their_image_policy(self):
        from local_databases import PGVECTOR_IMAGE
        for extensions, image in [(None, 'postgres:17-alpine'), (['vector'], PGVECTOR_IMAGE), (['uuid-ossp'], 'postgres:17-alpine')]:
            config = self.config()
            dep = config['dependencies']['db']
            dep.pop('initSql')
            if extensions is None:
                dep.pop('extensions')
            else:
                dep['extensions'] = extensions
            docs = list(yaml.safe_load_all(render_plan(self.plan(config), 'regular', 'test/app:1')[0]))
            pod = next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'db')
            self.assertEqual(pod['spec']['template']['spec']['containers'][0]['image'], image)
            self.assertEqual(any(d['kind'] == 'ConfigMap' for d in docs), extensions is not None)

    def test_sql_files_use_checked_in_order_not_duplicate_basename_order(self):
        (self.root / 'next').mkdir()
        (self.root / 'next/schema.sql').write_text('INSERT INTO marker VALUES (1);')
        config = self.config()
        config['dependencies']['db']['initSql'].append('next/schema.sql')
        docs = list(yaml.safe_load_all(render_plan(self.plan(config), 'ordered', 'test/app:1')[0]))
        sql = next(d for d in docs if d['kind'] == 'ConfigMap')['data']
        self.assertTrue(sql['01-repository.sql'].startswith('CREATE TABLE'))
        self.assertTrue(sql['02-repository.sql'].startswith('INSERT'))

    def test_invalid_or_escaping_sql_paths_are_configuration_errors(self):
        for paths in [[], ['schema.sql'] * 5, ['/etc/passwd'], ['../secret.sql'], ['.git/config.sql'], ['missing.sql'], ['Dockerfile'], [None], 'schema.sql']:
            config = self.config()
            config['dependencies']['db']['initSql'] = paths
            with self.subTest(paths=paths), self.assertRaises(ConfigurationRequired):
                self.plan(config)

    def test_only_small_utf8_sql_files_are_accepted(self):
        for raw in [b'x' * (64 * 1024 + 1), b'\xff', b'SELECT 1;\x00']:
            (self.root / 'schema.sql').write_bytes(raw)
            with self.subTest(size=len(raw)), self.assertRaises(ConfigurationRequired):
                self.plan()

    def test_cumulative_size_limit_and_symlink_escape(self):
        (self.root / 'schema.sql').write_bytes(b'x' * (33 * 1024))
        config = self.config()
        config['dependencies']['db']['initSql'] *= 2
        with self.assertRaisesRegex(ConfigurationRequired, '64 KiB'):
            self.plan(config)
        # inside() resolves symlinks before the file is read.
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / 'secret.sql'
            secret.write_text('SELECT 1;')
            try:
                (self.root / 'link.sql').symlink_to(secret)
            except OSError:
                return  # Windows without symlink privilege still checks ../ and absolute paths above.
            config['dependencies']['db']['initSql'] = ['link.sql']
            with self.assertRaises(ConfigurationRequired):
                self.plan(config)

    def test_unsupported_extensions_and_non_postgres_initializers_are_rejected(self):
        for extensions in [['postgis', 'vector'], ['postgis', 'postgis'], ['arbitrary'], [], 'postgis', [None]]:
            with self.subTest(extensions=extensions), self.assertRaises(ConfigurationRequired):
                normalize_dependencies({'db': {'type': 'postgres', 'extensions': extensions}})
        with self.assertRaises(ConfigurationRequired):
            normalize_dependencies({'db': {'type': 'redis', 'initSql': ['schema.sql']}}, self.root)

    def test_service_group_shares_one_initialized_database_and_configmap(self):
        config = self.config()
        config.pop('service')
        config.update(version=2, services={'gateway': {'port': 8000}, 'api': {'port': 8000}},
                      entryService='gateway', targetService='api')
        plan = resolve_services(self.root, config)
        docs = list(yaml.safe_load_all(render_services(plan, 'request-group', {'gateway': 'g:1', 'api': 'a:1'})[0]))
        self.assertEqual(len([d for d in docs if d['kind'] == 'ConfigMap']), 1)
        self.assertEqual(len([d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'db']), 1)


if __name__ == '__main__':
    unittest.main()
