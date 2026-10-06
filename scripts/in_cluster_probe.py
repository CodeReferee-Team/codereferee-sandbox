"""Observe Service traffic from a request-local Pod, without bypassing its network.

Only the observer is port-forwarded. The measured HTTP request originates inside
the cluster and crosses the target Service and Pod interface (including netem).
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from collect_baseline import kubectl_command, kubectl_environment
from run_pod_kill_experiment import stop_process


OBSERVER_CODE = '''import json, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import build_opener, ProxyHandler, Request
from urllib.error import HTTPError
import os
url=os.environ["TARGET_URL"]
timeout=float(os.environ["PROBE_TIMEOUT"])
opener=build_opener(ProxyHandler({}))
spec=json.loads(os.environ.get("PROBE_SPEC","{}"))
context=json.loads(os.environ.get("PROBE_CONTEXT","{}"))
def render(value):
 if isinstance(value,str):
  for key,item in context.items(): value=value.replace("${"+key+"}",str(item))
  return value
 if isinstance(value,dict): return {k:render(v) for k,v in value.items()}
 if isinstance(value,list): return [render(v) for v in value]
 return value
def call(step):
 step=render(step)
 body=json.dumps(step["body"]).encode() if "body" in step else None
 headers={"Content-Type":"application/json",**step.get("headers",{})}
 request=Request(url.rstrip("/")+step.get("path","/"),data=body,headers=headers,method=step.get("method","GET"))
 with opener.open(request,timeout=timeout) as response:
  payload=json.loads(response.read() or b"null")
  for key,path in step.get("extract",{}).items():
   value=payload
   for part in path.split("."): value=value[part]
   context[key]=value
  return response.status
for step in spec.get("setup",[]): call(step)
class Handler(BaseHTTPRequestHandler):
 protocol_version="HTTP/1.1"
 def do_GET(self):
  started=time.monotonic(); status=None; error=None
  try:
   if spec.get("steps"):
    for step in spec["steps"]:
     status=None
     status=call(step)
   else:
    with opener.open(url,timeout=timeout) as response:
     status=response.status; response.read(1024)
  except HTTPError as exc: status=exc.code; error=str(exc)
  except Exception as exc: error=str(exc)
  body=json.dumps({"at":datetime.now(timezone.utc).isoformat(),"status_code":status,"latency_ms":round((time.monotonic()-started)*1000,2),"success":error is None and status is not None and 200<=status<400,"error":error}).encode()
  self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.send_header("Connection","close"); self.end_headers(); self.wfile.write(body); self.wfile.flush(); self.close_connection=True
 def log_message(self,*args): pass
HTTPServer(("0.0.0.0",8080),Handler).serve_forever()
'''


class InClusterProbe:
    def __init__(self, namespace: str, service: str, port: int, *, timeout: float = 2,
                 local_port: int = 18080, path: str = '/', spec: dict | None = None,
                 context: dict | None = None):
        self.namespace = namespace
        self.name = 'codereferee-observer-' + uuid.uuid4().hex[:8]
        self.url = f'http://{service}.{namespace}.svc.cluster.local:{port}{path}'
        self.timeout = timeout
        self.local_port = local_port
        self.forward = None
        self.spec = spec or {}
        self.context = context or {}
        self.log_handles = []

    def __enter__(self):
        manifest = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {
            'name': self.name, 'namespace': self.namespace,
            'labels': {'app.kubernetes.io/name': 'codereferee-observer'}}, 'spec': {
            'automountServiceAccountToken': False, 'restartPolicy': 'Never',
            'containers': [{'name': 'observer', 'image': 'python:3.12-alpine',
                'command': ['python', '-u', '-c', OBSERVER_CODE],
                'env': [{'name': 'TARGET_URL', 'value': self.url},
                        {'name': 'PROBE_TIMEOUT', 'value': str(self.timeout)},
                        {'name': 'PROBE_SPEC', 'value': json.dumps(self.spec)},
                        {'name': 'PROBE_CONTEXT', 'value': json.dumps(self.context)}],
                'resources': {'requests': {'cpu': '25m', 'memory': '32Mi'},
                              'limits': {'cpu': '200m', 'memory': '96Mi'}},
                'readinessProbe': {'tcpSocket': {'port': 8080}, 'periodSeconds': 1}}]}}
        try:
            self.run('apply', '-f', '-', input_text=json.dumps(manifest))
            self.run('wait', '--for=condition=Ready', f'pod/{self.name}', '--timeout=120s')
            # kubectl emits a line for every connection. Undrained PIPE buffers
            # can block port-forward on Windows during a long observation.
            log_dir = Path(__file__).resolve().parents[1] / '.runtime' / 'probe-logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            self.log_handles = [(log_dir / f'{self.name}.{stream}.log').open('w', encoding='utf-8')
                                for stream in ('stdout', 'stderr')]
            self.forward = subprocess.Popen(kubectl_command('-n', self.namespace, 'port-forward',
                f'pod/{self.name}', f'{self.local_port}:8080'), stdout=self.log_handles[0],
                stderr=self.log_handles[1], text=True, env=kubectl_environment())
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    self.request_once()
                    return self
                except (OSError, ValueError):
                    if self.forward.poll() is not None:
                        raise RuntimeError('Observer port-forward failed; inspect .runtime/probe-logs.')
                    time.sleep(0.2)
            raise RuntimeError('Observer port-forward did not become available.')
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def probe(self) -> dict:
        errors = []
        for attempt in range(3):
            try:
                result = self.request_once()
                if errors:
                    result['observer_transport_errors'] = errors
                    result['observer_transport_retries'] = len(errors)
                return result
            except OSError as exc:
                errors.append(f'{type(exc).__name__}: {exc}')
                if attempt < 2:
                    time.sleep(0.2)
        raise RuntimeError('Observer transport failed after 3 attempts: ' + '; '.join(errors))

    def request_once(self) -> dict:
        with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{self.local_port}/',
                                               timeout=self.timeout + 5) as response:
            return json.loads(response.read())

    def collect(self, count: int) -> list[dict]:
        return [self.probe() for _ in range(count)]

    def run(self, *args, input_text=None):
        result = subprocess.run(kubectl_command('-n', self.namespace, *args),
            input=input_text, text=True, capture_output=True, env=kubectl_environment(), timeout=130)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        return result

    def __exit__(self, *args):
        if self.forward:
            stop_process(self.forward)
        for handle in self.log_handles:
            handle.close()
        subprocess.run(kubectl_command('-n', self.namespace, 'delete', 'pod', self.name,
            '--ignore-not-found=true', '--wait=false'), capture_output=True,
            text=True, env=kubectl_environment(), timeout=20)
