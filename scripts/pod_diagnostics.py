"""Best-effort, bounded deployment evidence collected before namespace cleanup.

Diagnostics are untrusted application output, not instructions or a verdict.
Never dump Pod specs/env or Kubernetes Secret objects.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone

from collect_baseline import kubectl_command, kubectl_environment
from pod_selection import owned_pods

MAX_PODS = 4
MAX_CONTAINERS = 4
MAX_EVENTS = 12
MAX_TEXT = 6000
TOTAL_SECONDS = 25


def redact(text: str, secret_values: tuple[str, ...] = ()) -> str:
    for value in sorted(set(secret_values), key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(?i)(https?://|redis://|postgres(?:ql)?://|mysql://)([^\s/@]+)@',
                  r'\1[REDACTED]@', text)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*(?:bearer\s+|basic\s+)?)[^\s,;]+',
                  r'\1[REDACTED]', text)
    text = re.sub(r'''(?ix)((?:password|passwd|token|api[_-]?key|secret)\s*["']?\s*[:=]\s*)
                     (?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)''', r'\1[REDACTED]', text)
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
                  '[REDACTED PRIVATE KEY]', text, flags=re.S)
    return text[-MAX_TEXT:]


def collect_pod_diagnostics(namespace: str, deployment: str, secret_values: tuple[str, ...] = ()) -> dict:
    report = {'pods': [], 'collected_at': datetime.now(timezone.utc).isoformat(),
              'collection_errors': [], 'truncated': False}
    deadline = time.monotonic() + TOTAL_SECONDS

    def execute(*args: str, optional: bool = False) -> str | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if 'collection_deadline_exceeded' not in report['collection_errors']:
                report['collection_errors'].append('collection_deadline_exceeded')
            return None
        try:
            completed = subprocess.run(kubectl_command(*args), capture_output=True, text=True,
                encoding='utf-8', errors='replace', env=kubectl_environment(),
                timeout=min(5, remaining))
            if completed.returncode:
                if not optional:
                    # Do not forward CLI stderr: it may contain credentials or unrelated data.
                    report['collection_errors'].append('kubectl_' + args[0] + '_failed')
                return None
            return completed.stdout
        except (OSError, subprocess.TimeoutExpired):
            report['collection_errors'].append('kubectl_' + args[0] + '_unavailable')
            return None

    def read_json(*args: str) -> dict:
        value = execute(*args)
        if value is None:
            return {}
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            report['collection_errors'].append('invalid_kubectl_json')
            return {}

    resource = read_json('get', 'deployment', deployment, '-n', namespace, '-o', 'json')
    replicas = read_json('get', 'replicasets', '-n', namespace, '-o', 'json')
    listing = read_json('get', 'pods', '-n', namespace, '-o', 'json')
    try:
        pods = owned_pods(resource, replicas.get('items', []), listing.get('items', []))
    except (RuntimeError, KeyError, TypeError):
        report['collection_errors'].append('deployment_ownership_unverified')
        return report
    pods.sort(key=lambda item: item.get('metadata', {}).get('name', ''))
    report['truncated'] = len(pods) > MAX_PODS
    for pod in pods[:MAX_PODS]:
        metadata, status = pod.get('metadata', {}), pod.get('status', {})
        name, uid = metadata.get('name'), metadata.get('uid')
        entry = {'name': name, 'uid': uid, 'phase': status.get('phase'),
                 'containers': [], 'logs_tail': '', 'events': []}
        states = status.get('containerStatuses', [])
        report['truncated'] |= len(states) > MAX_CONTAINERS
        tails = []
        for state in states[:MAX_CONTAINERS]:
            terminated = state.get('lastState', {}).get('terminated', {})
            current = state.get('state', {}).get('terminated', {})
            container = {'name': state.get('name'), 'ready': state.get('ready', False),
                'restart_count': state.get('restartCount', 0),
                'waiting_reason': state.get('state', {}).get('waiting', {}).get('reason'),
                'last_terminated': {key: terminated.get(key) for key in ('reason', 'exitCode')},
                'terminated': {'reason': current.get('reason'), 'exit_code': current.get('exitCode')}}
            container['last_terminated']['exit_code'] = container['last_terminated'].pop('exitCode')
            base = ('logs', name, '-n', namespace, '-c', state['name'], '--tail=50', '--limit-bytes=6000')
            logs = execute(*base, '--previous', optional=True) if state.get('restartCount', 0) else None
            container['logs_source'] = 'previous' if logs is not None else 'current'
            if logs is None:
                logs = execute(*base, optional=True)
            container['logs_available'] = logs is not None
            if logs is None:
                container['logs_source'] = None
            container['logs_tail'] = redact('\n'.join((logs or '').splitlines()[-50:]), secret_values)
            tails.extend(container['logs_tail'].splitlines())
            entry['containers'].append(container)
        entry['logs_tail'] = '\n'.join(tails[-50:])[-MAX_TEXT:]
        events = read_json('get', 'events', '-n', namespace, '--field-selector',
                          f'involvedObject.uid={uid}', '-o', 'json').get('items', [])
        events = [event for event in events if event.get('involvedObject', {}).get('uid') == uid]
        events.sort(key=lambda event: event.get('lastTimestamp') or event.get('metadata', {}).get('creationTimestamp', ''))
        report['truncated'] |= len(events) > MAX_EVENTS
        entry['events'] = [{'reason': event.get('reason'),
            'message': redact(str(event.get('message', '')), secret_values)[-1000:],
            'count': event.get('count')} for event in events[-MAX_EVENTS:]]
        report['pods'].append(entry)
    return report
