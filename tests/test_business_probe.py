import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import socket
from urllib.request import urlopen
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from business_probe import CLIENT_CODE, validate_probe_spec
from execution_plan import ConfigurationRequired
from in_cluster_probe import OBSERVER_CODE, InClusterProbe
from run_load_experiment import GENERATOR_CODE
from app.main import ChaosTarget, probe_environment, strip_private_probe_specs


class BusinessProbeTests(unittest.TestCase):
    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"data":{"accessToken":"dummy-token"}}')
            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302); self.send_header('Location', '/private'); self.end_headers(); return
                code = 200 if self.headers.get('Authorization') == 'Bearer dummy-token' else 401
                self.send_response(code); self.end_headers(); self.wfile.write(b'{"ok":true}')
            def log_message(self, *args): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.spec = {'setup': [{'path':'/login','method':'POST','body':{'email':'${runId}@example.test'},
                              'extract':{'token':'data.accessToken'}}],
                     'steps':[{'path':'/private','headers':{'Authorization':'Bearer ${token}'}, 'expect':{'ok':True}}]}

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def client(self, spec):
        values = {}; exec(CLIENT_CODE, values)
        return values['WorkflowClient'](self.url, 1, validate_probe_spec(spec))

    def test_api_imports_in_a_clean_process_without_scripts_on_pythonpath(self):
        root=Path(__file__).resolve().parents[1]
        code='import sys;sys.path.insert(0,sys.argv[1]);import app.main;print("API_IMPORT_OK")'
        result=subprocess.run([sys.executable,'-I','-c',code,str(root)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('API_IMPORT_OK',result.stdout)

    def test_real_login_extract_authorization_assertion_and_no_redirect(self):
        client = self.client(self.spec); client.setup()
        self.assertEqual(client.measured(), (200, True, None))
        client.spec['steps'][0]['expect']['ok'] = False
        self.assertFalse(client.measured()[1])
        self.assertEqual(self.client({'steps':[{'path':'/redirect'}]}).measured()[0:2], (302, False))
        self.assertEqual(self.client({'steps':[{'path':'/private'}]}).measured()[0:2], (401, False))

    def test_invalid_origins_headers_and_oversized_specs_are_rejected(self):
        for step in ({'path':'http://other/'}, {'path':'//other/'}, {'path':'/','headers':{'Host':'other'}},
                     {'path':'/','headers':{'X-Test':'a\r\nb'}}, {'path':'/','method':'CONNECT'}):
            with self.subTest(step=step), self.assertRaises(ConfigurationRequired):
                validate_probe_spec({'steps':[step]})
        with self.assertRaises(ConfigurationRequired): validate_probe_spec({'steps':[{'path':'/'}]*9})
        with self.assertRaises(ConfigurationRequired): validate_probe_spec({'steps':[]})

    def test_environment_contract_retains_spec_and_strict_success(self):
        target = ChaosTarget(namespace='n', deployment='d', service='s', servicePort=80, labelSelector='a=b', probeSpec=self.spec)
        env = probe_environment(target)
        self.assertEqual(json.loads(env['CODEREFEREE_PROBE_SPEC']), self.spec)
        self.assertEqual(env['CODEREFEREE_PROBE_SUCCESS_BELOW'], '300')
        with patch.dict(os.environ, env): self.assertEqual(InClusterProbe('n','s',80).spec, self.spec)

    def test_dependency_path_override_is_not_replaced_by_parent_workflow(self):
        with patch.dict(os.environ, {'CODEREFEREE_PROBE_SPEC':json.dumps(self.spec)}):
            observer = InClusterProbe('n','s',80,path='/redis-business',spec={})
        self.assertEqual(observer.spec,{})
        self.assertTrue(observer.url.endswith('/redis-business'))

    def test_public_result_hides_specs_in_dependency_and_peer_targets_too(self):
        result = {'deployment':{'target':{'probeSpec':self.spec,'dependencies':{'db':{'probeSpec':self.spec}}},
                   'services':{'api':{'dependencies':{'db':{'probeSpec':self.spec}}}}},'metrics':{'availability':1}}
        strip_private_probe_specs(result)
        self.assertNotIn('dummy-token', json.dumps(result))
        self.assertNotIn('probeSpec', json.dumps(result))
        self.assertEqual(result['metrics']['availability'], 1)

    def test_observer_setup_failure_exposes_only_safe_stage_and_status(self):
        def execute(*args, input_text=None):
            if args[0] == 'get':
                return subprocess.CompletedProcess([],0,json.dumps({'status':{'phase':'Failed',
                    'containerStatuses':[{'name':'observer','state':{'terminated':{'exitCode':42}}}]}}),'')
            if args[0] == 'logs':
                return subprocess.CompletedProcess([],0,json.dumps({'probe_setup_error':{'step':0,'status_code':401,
                                                                                       'token':'do-not-publish'}}),'')
            return subprocess.CompletedProcess([],0,'','')
        observer = InClusterProbe('n','s',80,spec=self.spec)
        with patch.object(observer,'run',side_effect=execute), patch.object(observer,'__exit__') as cleanup:
            with self.assertRaises(RuntimeError) as caught: observer.__enter__()
        self.assertIn('401',str(caught.exception))
        self.assertNotIn('do-not-publish',str(caught.exception))
        cleanup.assert_called_once()

    def test_observer_waits_for_actual_ready_condition(self):
        responses = [subprocess.CompletedProcess([],0,json.dumps({'status':{'phase':'Pending'}}),''),
                     subprocess.CompletedProcess([],0,json.dumps({'status':{'phase':'Running','conditions':[
                         {'type':'Ready','status':'True'}]}}),'')]
        observer = InClusterProbe('n','s',80)
        with patch.object(observer,'run',side_effect=responses) as read, patch('in_cluster_probe.time.sleep'):
            observer.wait_ready()
        self.assertEqual(read.call_count,2)

    def test_real_generator_uses_auth_and_counts_unauthorized_business_as_failure(self):
        env = dict(os.environ, TARGET_URL=self.url, STAGES='2', STAGE_SECONDS='0.3', THINK_SECONDS='0.05', REQUEST_TIMEOUT='1')
        for spec, expect_failure in ((self.spec, False), ({'steps':[{'path':'/private'}]}, True)):
            with self.subTest(expect_failure=expect_failure):
                result = subprocess.run([sys.executable, '-c', GENERATOR_CODE], env=dict(env, PROBE_SPEC=json.dumps(spec)),
                                        capture_output=True, text=True, timeout=15, check=True)
                stage = json.loads(result.stdout)['stages'][0]
                self.assertGreater(stage['requests'], 0)
                self.assertEqual(stage['failed_requests'] > 0, expect_failure)
                self.assertIsNotNone(stage['p95_latency_ms'])
                self.assertNotIn('dummy-token', result.stdout + result.stderr)

    def test_real_observer_process_runs_setup_and_returns_only_measurements(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        code = OBSERVER_CODE.replace('HTTPServer(("0.0.0.0",8080)', f'HTTPServer(("127.0.0.1",{port})')
        process = subprocess.Popen([sys.executable,'-c',code], env=dict(os.environ,
            TARGET_URL=self.url,PROBE_TIMEOUT='1',PROBE_SPEC=json.dumps(self.spec)),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+10
            while True:
                try:
                    with urlopen(f'http://127.0.0.1:{port}/',timeout=2) as response: raw=response.read()
                    break
                except OSError:
                    if time.monotonic()>deadline: self.fail('Observer did not start')
                    time.sleep(0.05)
            result=json.loads(raw)
            self.assertTrue(result['success'])
            self.assertEqual(result['status_code'],200)
            self.assertNotIn(b'dummy-token',raw)
        finally:
            process.terminate(); process.communicate(timeout=5)


if __name__ == '__main__': unittest.main()
