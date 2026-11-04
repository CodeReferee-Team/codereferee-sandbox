"""Bounded same-origin HTTP workflows shared by the observer and load generator."""
import json
import re

try:
    from .execution_plan import ConfigurationRequired
except ImportError:
    from execution_plan import ConfigurationRequired


def validate_probe_spec(spec):
    if spec is None:
        return None
    if not isinstance(spec, dict) or set(spec) - {'setup', 'steps'} or not spec.get('steps'):
        raise ConfigurationRequired('probeSpec requires measured steps and optional setup.')
    if len(json.dumps(spec)) > 16384:
        raise ConfigurationRequired('probeSpec exceeds 16 KiB.')
    for phase in ('setup', 'steps'):
        steps = spec.get(phase, [])
        if not isinstance(steps, list) or len(steps) > 8:
            raise ConfigurationRequired('A probe phase supports at most eight steps.')
        for step in steps:
            if not isinstance(step, dict) or set(step) - {'path', 'method', 'body', 'headers', 'extract', 'expect'}:
                raise ConfigurationRequired('Unknown probe step field.')
            path = step.get('path', '/')
            if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or any(c in path for c in ('\r', '\n', '\\', '#')):
                raise ConfigurationRequired('Probe paths must remain on the declared Service origin.')
            if not isinstance(step.get('method', 'GET'), str) or step.get('method', 'GET') not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
                raise ConfigurationRequired('Unsupported probe HTTP method.')
            headers = step.get('headers', {})
            if not isinstance(headers, dict) or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9-]+', k)
                or k.lower() in {'host', 'connection', 'content-length', 'transfer-encoding'}
                or not isinstance(v, str) or '\r' in v or '\n' in v for k, v in headers.items()):
                raise ConfigurationRequired('Invalid probe headers.')
            for key in ('extract', 'expect'):
                values = step.get(key, {})
                if not isinstance(values, dict) or len(values) > 16:
                    raise ConfigurationRequired('Probe extraction/assertions must be small mappings.')
                if key == 'extract' and any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)
                    or not isinstance(path, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', path) for name, path in values.items()):
                    raise ConfigurationRequired('Invalid probe extraction path.')
                if key == 'expect' and any(not isinstance(path, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', path) for path in values):
                    raise ConfigurationRequired('Invalid probe assertion path.')
    return spec


def environment_probe_spec():
    import os
    text = os.getenv('CODEREFEREE_PROBE_SPEC')
    return validate_probe_spec(json.loads(text)) if text else None


# Embedded in the existing Python-only observer/generator images. No pip dependency.
# Never follow redirects, emit response bodies, or include credentials in error text.
CLIENT_CODE = r'''
import http.client,json,uuid
from urllib.parse import urlsplit
class WorkflowClient:
 def __init__(self,url,timeout,spec,context=None):
  self.url=urlsplit(url); self.timeout=timeout; self.spec=spec
  self.context={"runId":uuid.uuid4().hex,**(context or {})}
 def render(self,value):
  if isinstance(value,str):
   for key,item in self.context.items(): value=value.replace("${"+key+"}",str(item))
   if "${" in value: raise ValueError("Unresolved probe variable")
   return value
  if isinstance(value,dict): return {k:self.render(v) for k,v in value.items()}
  if isinstance(value,list): return [self.render(v) for v in value]
  return value
 def value(self,payload,path):
  for part in path.split("."): payload=payload[int(part)] if isinstance(payload,list) else payload[part]
  return payload
 def call(self,step):
  step=self.render(step); path=step.get("path","/")
  if not path.startswith("/") or path.startswith("//") or "\r" in path or "\n" in path: raise ValueError("Invalid probe path")
  body=json.dumps(step["body"]).encode() if "body" in step else None
  headers={"Content-Type":"application/json","Connection":"close",**step.get("headers",{})}
  connection=http.client.HTTPConnection(self.url.hostname,self.url.port or 80,timeout=self.timeout)
  try:
   connection.request(step.get("method","GET"),path,body=body,headers=headers)
   response=connection.getresponse(); status=response.status; raw=response.read(1048577)
   if len(raw)>1048576: raise ValueError("Probe response exceeds limit")
   if not 200<=status<300: return status,False
   if step.get("extract") or step.get("expect"):
    payload=json.loads(raw)
    for key,path in step.get("extract",{}).items(): self.context[key]=self.value(payload,path)
    for path,value in step.get("expect",{}).items():
     if self.value(payload,path)!=value: return status,False
   return status,True
  finally: connection.close()
 def setup(self):
  for step in self.spec.get("setup",[]):
   if not self.call(step)[1]: raise RuntimeError("Business probe setup failed")
 def measured(self):
  status=None
  try:
   for step in self.spec["steps"]:
    status=None
    status,success=self.call(step)
    if not success: return status,False,"Business response rejected"
   return status,True,None
  except Exception as exc: return status,False,type(exc).__name__
'''
