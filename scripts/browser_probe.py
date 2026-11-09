"""Bounded declarative browser journeys in request-local, disposable Pods."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from uuid import uuid4

try:
    from .execution_plan import ConfigurationRequired
    from .collect_baseline import kubectl_command, kubectl_environment
except ImportError:
    from execution_plan import ConfigurationRequired
    from collect_baseline import kubectl_command, kubectl_environment


BROWSER_IMAGE = 'codereferee/browser-probe:pw1.64.1-v1'
PUBLIC_FIELDS = {'schemaVersion', 'observationStatus', 'exitCode', 'completedSteps', 'failedStep',
                 'errorCode', 'pageErrorCount', 'blockedRequestCount', 'httpStatusCounts',
                 'browserVersion', 'durationMillis'}


def local_path(path):
    return (isinstance(path, str) and path.startswith('/') and not path.startswith('//')
            and len(path) <= 512 and not any(c in path for c in ('\\', '\r', '\n', '#')))


def validate_browser_probe(spec):
    if spec is None:
        return None
    try:
        size = len(json.dumps(spec, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ConfigurationRequired('browserProbe must contain JSON values.') from exc
    if not isinstance(spec, dict) or size > 16384 or set(spec) - {'steps', 'timeoutSeconds', 'stepTimeoutSeconds'}:
        raise ConfigurationRequired('browserProbe accepts steps and bounded journey/step timeouts, up to 16 KiB.')
    for field, default, lower, upper in (('timeoutSeconds', 30, 5, 60), ('stepTimeoutSeconds', 5, 1, 15)):
        value = spec.get(field, default)
        if type(value) is not int or not lower <= value <= upper:
            raise ConfigurationRequired(f'{field} must be an integer in {lower}..{upper}.')
    steps = spec.get('steps')
    if not isinstance(steps, list) or not 2 <= len(steps) <= 16:
        raise ConfigurationRequired('A browser journey requires 2..16 steps.')
    fields = {'goto': {'action', 'path'}, 'fill': {'action', 'selector', 'value'},
              'click': {'action', 'selector', 'expectResponse'},
              'assertText': {'action', 'selector', 'text'}, 'assertVisible': {'action', 'selector'}}
    for step in steps:
        if (not isinstance(step, dict) or not isinstance(step.get('action'), str)
                or step['action'] not in fields or set(step) - fields[step['action']]):
            raise ConfigurationRequired('Unsupported browser action or field.')
        action = step['action']
        if action == 'goto':
            if not local_path(step.get('path')):
                raise ConfigurationRequired('Browser navigation must stay on the entry Service origin.')
        else:
            selector = step.get('selector')
            if not isinstance(selector, str) or not 1 <= len(selector) <= 256 or re.search(r'[\x00-\x1f]', selector):
                raise ConfigurationRequired('Browser selectors must be bounded nonempty strings.')
        for field in ({'fill': ['value'], 'assertText': ['text']}.get(action, [])):
            if not isinstance(step.get(field), str) or not 1 <= len(step[field]) <= 512:
                raise ConfigurationRequired('Browser values and text assertions must be bounded strings.')
        if 'expectResponse' in step:
            response = step['expectResponse']
            if not isinstance(response, dict) or set(response) - {'path', 'status', 'expect'} or not local_path(response.get('path')):
                raise ConfigurationRequired('expectResponse requires a local path and HTTP status.')
            if type(response.get('status')) is not int or not 100 <= response['status'] <= 599:
                raise ConfigurationRequired('Expected HTTP status must be in 100..599.')
            expected = response.get('expect', {})
            if not isinstance(expected, dict) or len(expected) > 16 or any(
                    not isinstance(p, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in expected):
                raise ConfigurationRequired('Browser JSON assertions must be small path mappings.')
    if steps[0].get('action') != 'goto' or not any(s['action'] in {'assertText', 'assertVisible'} for s in steps):
        raise ConfigurationRequired('Browser journeys start with goto and require a visible DOM assertion.')
    return spec


# No DOM, response body, selector, entered value, URL query or browser exception
# text is returned. Reproduction credentials stay inside the disposable Pod.
BROWSER_CODE = r'''
import json,os,time,uuid
from urllib.parse import urlsplit
started=time.monotonic()
report={'schemaVersion':'browser-probe.v1','observationStatus':'infrastructure_error',
        'exitCode':None,'completedSteps':0,'failedStep':None,'errorCode':None,
        'pageErrorCount':0,'blockedRequestCount':0,'httpStatusCounts':{}}
def emit():
    report['durationMillis']=round((time.monotonic()-started)*1000)
    print(json.dumps(report),flush=True)
try:
    from playwright.sync_api import sync_playwright,expect,TimeoutError as BrowserTimeout
except ImportError:
    report['errorCode']='browser_runtime_missing';emit();raise SystemExit(2)
spec=json.loads(os.environ['BROWSER_SPEC']);base=os.environ['TARGET_URL'].rstrip('/')
origin=urlsplit(base);run_id=uuid.uuid4().hex
def same_origin(url):
    parsed=urlsplit(url)
    return (parsed.scheme,parsed.hostname,parsed.port or 80)==(origin.scheme,origin.hostname,origin.port or 80)
def render(value):
    if isinstance(value,str):return value.replace('${runId}',run_id)
    if isinstance(value,dict):return {k:render(v) for k,v in value.items()}
    if isinstance(value,list):return [render(v) for v in value]
    return value
def lookup(value,path):
    for part in path.split('.'):
        value=value[int(part)] if isinstance(value,list) else value[part]
    return value
os.makedirs('/tmp/browser-home',exist_ok=True)
with sync_playwright() as playwright:
    try:
        browser=playwright.chromium.launch(headless=True,chromium_sandbox=True)
    except Exception:
        report['errorCode']='browser_launch_failed';emit();raise SystemExit(2)
    try:
        report['browserVersion']=browser.version
        context=browser.new_context(service_workers='block',accept_downloads=False)
        def route_request(route):
            try:
                permitted=same_origin(route.request.url)
            except ValueError:
                permitted=False
            if permitted:route.continue_()
            else:
                report['blockedRequestCount']+=1;route.abort()
        context.route('**/*',route_request)
        context.route_web_socket('**/*',lambda socket:socket.close())
        page=context.new_page()
        def page_error(_):report['pageErrorCount']+=1
        def response_event(response):
            key=str(response.status)
            report['httpStatusCounts'][key]=report['httpStatusCounts'].get(key,0)+1
        page.on('pageerror',page_error);page.on('response',response_event)
        deadline=time.monotonic()+spec.get('timeoutSeconds',30)
        report.update(observationStatus='observed',exitCode=1)
        for index,raw in enumerate(spec['steps']):
            report['failedStep']=index
            step=render(raw);remaining=deadline-time.monotonic()
            if remaining<=0:raise BrowserTimeout('journey deadline')
            milliseconds=max(1,min(spec.get('stepTimeoutSeconds',5),remaining)*1000)
            page.set_default_timeout(milliseconds);page.set_default_navigation_timeout(milliseconds)
            action=step['action']
            if action=='goto':
                response=page.goto(base+step['path'],wait_until='domcontentloaded')
                if response is None or not 200<=response.status<300:raise AssertionError('navigation status')
            elif action=='fill':page.locator(step['selector']).fill(step['value'])
            elif action=='click':
                expected=step.get('expectResponse')
                if expected:
                    with page.expect_response(lambda response:same_origin(response.url) and
                            response.url==base+expected['path'],timeout=milliseconds) as pending:
                        page.locator(step['selector']).click()
                    response=pending.value
                    if response.status!=expected['status']:raise AssertionError('response status')
                    if expected.get('expect'):
                        if len(response.body())>1048576:raise AssertionError('response size')
                        payload=response.json()
                        for path,value in expected['expect'].items():
                            if lookup(payload,path)!=value:raise AssertionError('response content')
                else:page.locator(step['selector']).click()
            elif action=='assertText':expect(page.locator(step['selector'])).to_contain_text(step['text'],timeout=milliseconds)
            elif action=='assertVisible':expect(page.locator(step['selector'])).to_be_visible(timeout=milliseconds)
            if not same_origin(page.url):raise AssertionError('navigation origin')
            report['completedSteps']+=1
        if report['blockedRequestCount']:raise AssertionError('external request')
        report.update(exitCode=0,failedStep=None)
    except BrowserTimeout:
        report['errorCode']='browser_action_timeout'
    except Exception:
        report['errorCode']='browser_assertion_failed'
    finally:
        browser.close()
emit()
raise SystemExit(0 if report['exitCode']==0 else 1)
'''


class BrowserProbeRunner:
    def __init__(self, target):
        for name in (target.namespace, target.probe_service or target.service):
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', name):
                raise ConfigurationRequired('Browser targets require valid namespace and Service DNS labels.')
        self.target = target
        self.image = os.getenv('CODEREFEREE_BROWSER_IMAGE', BROWSER_IMAGE)
        self.seccomp = {'type': 'RuntimeDefault'}
        profile = os.getenv('CODEREFEREE_BROWSER_SECCOMP_PROFILE')
        if profile:
            if profile.startswith('/') or '..' in profile.split('/') or not re.fullmatch(r'[A-Za-z0-9_./-]+', profile):
                raise ConfigurationRequired('Browser seccomp profile must be kubelet-relative.')
            self.seccomp = {'type': 'Localhost', 'localhostProfile': profile}

    def run(self, spec, *, timeout_seconds=150):
        spec = validate_browser_probe(spec)
        namespace = self.target.namespace
        name = 'codereferee-browser-' + uuid4().hex[:12]
        deadline = time.monotonic() + timeout_seconds
        service = self.target.probe_service or self.target.service
        port = self.target.probe_service_port or self.target.service_port
        url = f'http://{service}.{namespace}.svc.cluster.local:{port}'
        def command(*arguments, input_text=None):
            remaining = max(1, deadline - time.monotonic())
            return subprocess.run(kubectl_command('-n', namespace, *arguments), input=input_text,
                capture_output=True, text=True, encoding='utf-8', env=kubectl_environment(), timeout=min(15, remaining))
        report = {'schemaVersion': 'browser-probe.v1', 'observationStatus': 'infrastructure_error',
                  'exitCode': None, 'errorCode': 'browser_runtime_unavailable'}
        try:
            manifest = {'apiVersion': 'v1', 'kind': 'Pod',
                'metadata': {'name': name, 'namespace': namespace}, 'spec': {
                    'automountServiceAccountToken': False, 'restartPolicy': 'Never',
                    'activeDeadlineSeconds': max(1, int(deadline - time.monotonic())), 'terminationGracePeriodSeconds': 5,
                    'securityContext': {'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000,
                                        'runAsNonRoot': True, 'seccompProfile': self.seccomp},
                    'volumes': [{'name': 'scratch', 'emptyDir': {'sizeLimit': '256Mi'}}],
                    'containers': [{'name': 'browser', 'image': self.image, 'imagePullPolicy': 'IfNotPresent',
                        'command': ['python', '-u', '-c', BROWSER_CODE],
                        'env': [{'name': 'BROWSER_SPEC', 'value': json.dumps(spec)}, {'name': 'TARGET_URL', 'value': url}],
                        'resources': {'requests': {'cpu': '250m', 'memory': '256Mi'},
                                      'limits': {'cpu': '1000m', 'memory': '768Mi'}},
                        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                            'capabilities': {'drop': ['ALL']}},
                        'volumeMounts': [{'name': 'scratch', 'mountPath': '/tmp'}]}]}}
            applied = command('apply', '-f', '-', input_text=json.dumps(manifest))
            if applied.returncode:
                return report
            while time.monotonic() < deadline:
                result = command('get', 'pod', name, '-o', 'json')
                if result.returncode:
                    return report
                pod = json.loads(result.stdout)
                for container in pod.get('status', {}).get('containerStatuses', []):
                    if container.get('state', {}).get('waiting', {}).get('reason') in {
                            'ErrImagePull', 'ImagePullBackOff', 'InvalidImageName', 'CreateContainerConfigError'}:
                        return report
                if pod.get('status', {}).get('phase') in {'Succeeded', 'Failed'}:
                    logs = command('logs', name, '--limit-bytes=32768')
                    payload = json.loads(logs.stdout.strip().splitlines()[-1]) if logs.returncode == 0 and logs.stdout.strip() else {}
                    if not isinstance(payload, dict):
                        return report
                    code = pod.get('status', {}).get('containerStatuses', [{}])[0].get('state', {}).get('terminated', {}).get('exitCode')
                    if (payload.get('schemaVersion') == 'browser-probe.v1'
                            and payload.get('observationStatus') == 'observed'
                            and type(payload.get('exitCode')) is int and payload['exitCode'] in (0, 1)
                            and type(payload.get('completedSteps')) is int
                            and 0 <= payload['completedSteps'] <= len(spec['steps'])
                            and (payload['exitCode'] != 0 or payload['completedSteps'] == len(spec['steps']))
                            and payload.get('errorCode') in (None, 'browser_assertion_failed', 'browser_action_timeout')
                            and code == payload['exitCode']):
                        return {key: value for key, value in payload.items() if key in PUBLIC_FIELDS}
                    report['errorCode'] = (payload['errorCode'] if payload.get('errorCode') in
                        {'browser_runtime_missing', 'browser_launch_failed'} else 'browser_runner_failed')
                    return report
                time.sleep(0.5)
            report.update(errorCode='browser_runner_timeout', timedOut=True)
        except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError, IndexError):
            pass
        finally:
            try:
                removed = subprocess.run(kubectl_command('-n', namespace, 'delete', 'pod', name,
                    '--ignore-not-found=true', '--wait=false'), capture_output=True, timeout=15, env=kubectl_environment())
                report['podCleanupRequested'] = removed.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                report['podCleanupRequested'] = False
        return report
