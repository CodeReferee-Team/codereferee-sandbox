"""Request-scoped, container-cgroup OOM injection (no stress process in the app).

The helper lives outside the selected container's cgroup. It changes only the
exact container leaf, restores it in finally, and never changes a Pod/node limit.
Kubernetes OOMKilled evidence, not helper completion, proves target injection.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone

from collect_baseline import kubectl_command, kubectl_environment
from pod_selection import pod_evidence


HELPER_CODE = r'''
import json, os, re, signal, sys, time
from pathlib import Path
root=Path('/host-cgroup')
cid,uid=sys.argv[1:]
out={'container_id':cid,'pod_uid':uid,'mechanism':'cgroup_v2_memory_max'}
changed=False
limit=None
original=None
def stop(*args): raise RuntimeError('helper terminated')
signal.signal(signal.SIGTERM,stop)
try:
 if not re.fullmatch('[0-9a-f]{64}',cid): raise RuntimeError('invalid container ID')
 if not (root/'cgroup.controllers').is_file(): raise RuntimeError('cgroup v2 required')
 # Both systemd and cgroupfs drivers: exact leaf and exact owning Pod component.
 matches=[p for p in root.glob('**/*') if p.is_dir() and p.name in (cid,'cri-containerd-'+cid+'.scope')
          and any(x=='pod'+uid or x.endswith('-pod'+uid.replace('-','_')+'.slice') for x in p.parts)]
 if len(matches)!=1: raise RuntimeError('exact container cgroup not found uniquely')
 group=matches[0]
 if any(p.is_dir() for p in group.iterdir()): raise RuntimeError('target must be a leaf cgroup')
 limit=group/'memory.max'
 original=limit.read_text().strip()
 if original=='max' or not 32*1024**2<=int(original)<=4*1024**3:
  raise RuntimeError('finite target limit between 32Mi and 4Gi required')
 out.update(original_memory_max=int(original),injected_memory_max=1,cgroup=str(group.relative_to(root)))
 # The writer is outside this cgroup; only target processes can be OOM victims.
 # Set the flag before write, since the cgroup may disappear during the syscall.
 changed=True
 events=group/'memory.events.local'
 def oom_kills():
  return int(dict(line.split() for line in events.read_text().splitlines()).get('oom_kill',0))
 before_kills=oom_kills()
 out['oom_kill_before']=before_kills
 limit.write_text('1')
 out['limit_written']=True
 # Some kernels defer reclaim to the target's next charge. Keep the limit in
 # place until the target actually encounters OOM, rather than reverting early.
 deadline=time.monotonic()+15
 while time.monotonic()<deadline:
  try:
   out['oom_kill_after']=oom_kills()
   if out['oom_kill_after']>before_kills: break
  except FileNotFoundError: break
  time.sleep(0.1)
 else: out['error']='target_oom_not_observed'
except Exception as exc:
 out['error']=str(exc)
finally:
 if changed:
  try:
   limit.write_text(original)
   out['restore']='restored'
  except FileNotFoundError: out['restore']='target_cgroup_removed'
  except Exception as exc: out['restore']='failed';out['restore_error']=str(exc)
 else: out['restore']='not_changed'
 print(json.dumps(out),flush=True)
'''


def helper_manifest(namespace: str, name: str, pod: dict, container: str) -> dict:
    evidence = pod_evidence(pod, container)
    identifier = evidence.get('container_id') or ''
    if not re.fullmatch(r'containerd://[0-9a-f]{64}', identifier):
        raise ValueError('Target OOM requires a containerd container ID.')
    if not evidence.get('node') or not evidence.get('uid'):
        raise ValueError('Target OOM requires a scheduled Pod UID.')
    return {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {
        'namespace': namespace, 'name': name, 'labels': {'app.kubernetes.io/name': 'codereferee-oom-helper'}},
        'spec': {'nodeName': evidence['node'], 'restartPolicy': 'Never',
            'automountServiceAccountToken': False, 'activeDeadlineSeconds': 45,
            'terminationGracePeriodSeconds': 10,
            'containers': [{'name': 'injector', 'image': 'python:3.12-alpine', 'imagePullPolicy': 'IfNotPresent',
                'command': ['python', '-u', '-c', HELPER_CODE, identifier.split('://')[1], evidence['uid']],
                'securityContext': {'privileged': True, 'runAsUser': 0},
                'resources': {'requests': {'cpu': '25m', 'memory': '32Mi'}, 'limits': {'cpu': '200m', 'memory': '96Mi'}},
                'volumeMounts': [{'name': 'cgroup', 'mountPath': '/host-cgroup'}]}],
            'volumes': [{'name': 'cgroup', 'hostPath': {'path': '/sys/fs/cgroup', 'type': 'Directory'}}]}}


def oom_evidence(before: dict, after: dict, container: str, started_at: str) -> dict | None:
    if before['metadata']['uid'] != after.get('metadata', {}).get('uid'):
        return None
    previous = pod_evidence(before, container)
    current = next((s for s in after.get('status', {}).get('containerStatuses', []) if s.get('name') == container), {})
    termination = current.get('lastState', {}).get('terminated') or current.get('state', {}).get('terminated') or {}
    try:
        # kubelet termination timestamps can have only second resolution. Exact
        # container identity below excludes an old OOM from an earlier restart.
        recent = datetime.fromisoformat(termination['finishedAt'].replace('Z', '+00:00')) >= datetime.fromisoformat(started_at.replace('Z', '+00:00')).replace(microsecond=0)
    except (KeyError, ValueError):
        return None
    if (termination.get('reason') != 'OOMKilled' or termination.get('exitCode') != 137 or not recent
            or termination.get('containerID') != previous['container_id']):
        return None
    return {'pod': previous['name'], 'pod_uid': previous['uid'], 'container': container,
            'container_id': previous['container_id'], 'restart_count_before': previous['restart_count'],
            'restart_count_after': current.get('restartCount'), 'termination': termination}


def run_target_oom(namespace: str, pods: list[dict], container: str, observer,
                   timeout: float, started_at: str) -> tuple[dict, list[dict]]:
    if not 1 <= len(pods) <= 16:
        raise ValueError('Target OOM requires 1..16 explicitly selected Pods.')
    helpers, reports, evidence, probes = [], [], {}, []
    ready = False
    cleanup_errors = []

    def run(*args, input_text=None):
        return subprocess.run(kubectl_command('-n', namespace, *args), input=input_text,
            capture_output=True, text=True, check=True, timeout=15, env=kubectl_environment())

    error = None
    deadline = time.monotonic() + timeout
    try:
        for pod in pods:
            name = 'codereferee-oom-' + uuid.uuid4().hex[:10]
            manifest = helper_manifest(namespace, name, pod, container)
            helpers.append(name)  # apply can partially succeed
            run('apply', '-f', '-', input_text=json.dumps(manifest))
        finished = set()
        while time.monotonic() < deadline:
            probes.extend(observer.collect(1))
            ready = True
            for index, pod in enumerate(pods):
                current = json.loads(run('get', 'pod', pod['metadata']['name'], '-o', 'json').stdout)
                found = oom_evidence(pod, current, container, started_at)
                if found:
                    evidence[pod['metadata']['uid']] = found
                ready = ready and pod_evidence(current, container).get('ready') is True
                if index not in finished:
                    helper = json.loads(run('get', 'pod', helpers[index], '-o', 'json').stdout)
                    phase = helper.get('status', {}).get('phase')
                    if phase in ('Succeeded', 'Failed'):
                        report = json.loads(run('logs', helpers[index], '-c', 'injector').stdout)
                        reports.append(report)
                        finished.add(index)
            if any(r.get('restore') == 'failed' for r in reports):
                error = 'oom_restore_failed'
                break
            if any(r.get('error') or not r.get('limit_written') for r in reports):
                error = 'oom_not_triggered' if any(r.get('error') == 'target_oom_not_observed' for r in reports) else 'oom_injection_failed'
                break
            if (len(finished) == len(pods) and len(evidence) == len(pods) and ready
                    and len(probes) >= 5 and all(p['success'] for p in probes[-5:])):
                break
        if error is None and len(evidence) != len(pods):
            error = 'oom_not_triggered'
        if error is None and len(finished) != len(pods):
            error = 'oom_injection_failed'
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        error = 'oom_injection_failed'
        reports.append({'error': str(exc)[-1000:]})
    finally:
        for helper in helpers:
            try:
                run('delete', 'pod', helper, '--ignore-not-found', '--wait=true', '--timeout=12s')
            except (OSError, subprocess.SubprocessError):
                cleanup_errors.append(helper)
        if cleanup_errors:
            error = 'oom_helper_cleanup_failed'
    recovered = (ready and len(probes) >= 5 and all(p['success'] for p in probes[-5:])) if error is None else None
    return {'observationStatus': 'infrastructure_error' if error else 'observed',
            'exitCode': None if error else (0 if recovered else 1), 'infraError': error,
            'recovered': recovered, 'timedOut': error is None and not recovered,
            'oom_observed': len(evidence) == len(pods), 'target_oom_evidence': list(evidence.values()),
            'helper_reports': reports, 'helper_cleanup_errors': cleanup_errors}, probes
