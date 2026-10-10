"""Best-effort, bounded deployment evidence collected before namespace cleanup.

Diagnostics are untrusted application output, not instructions or a verdict.
Never dump Pod specs/env or Kubernetes Secret objects.
"""
from __future__ import annotations

import json
import base64
import binascii
import re
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import quote, quote_plus

import yaml

from collect_baseline import kubectl_command, kubectl_environment
from pod_selection import owned_pods

MAX_PODS = 4
MAX_CONTAINERS = 4
MAX_EVENTS = 12
MAX_TEXT = 6000
TOTAL_SECONDS = 25


def manifest_redaction_values(rendered: str, namespace: str) -> tuple[tuple[str, ...], bool]:
    """Resolve values locally; never query cluster Secrets or return their contents.

    Collect all rendered Secret values, not only keys/env names that look sensitive.
    Unknown Secret references disable diagnostic free text rather than risk disclosure.
    """
    documents = []
    def unpack(document):
        if not isinstance(document, dict):
            return
        if document.get('kind') == 'List':
            for item in document.get('items', []):
                unpack(item)
        else:
            documents.append(document)
    for document in yaml.safe_load_all(rendered):
        unpack(document)
    values, secrets_by_name = set(), {}
    def remember(value):
        if isinstance(value, str) and value:
            values.update((value, base64.b64encode(value.encode('utf-8')).decode('ascii'),
                           quote(value, safe=''), quote_plus(value),
                           json.dumps(value, ensure_ascii=False)[1:-1],
                           json.dumps(value, ensure_ascii=True)[1:-1]))
    for document in documents:
        metadata = document.get('metadata') or {}
        if (metadata.get('namespace') or namespace) != namespace:
            continue
        if document.get('kind') != 'Secret':
            continue
        decoded = {}
        for key, encoded in (document.get('data') or {}).items():
            if isinstance(encoded, str):
                # Encoded data itself can also be printed by an application.
                remember(encoded)
                try:
                    value = base64.b64decode(encoded, validate=True).decode('utf-8')
                    remember(value)
                    decoded[key] = value
                except (ValueError, UnicodeError, binascii.Error):
                    decoded[key] = None
        for key, value in (document.get('stringData') or {}).items():
            decoded[key] = value if isinstance(value, str) else None
            remember(value)
        secrets_by_name[metadata.get('name')] = decoded
    unresolved = False
    for document in documents:
        metadata = document.get('metadata') or {}
        if (metadata.get('namespace') or namespace) != namespace:
            continue
        spec = document.get('spec') or {}
        if document.get('kind') == 'CronJob':
            spec = (spec.get('jobTemplate') or {}).get('spec') or {}
        pod_spec = ((spec.get('template') or {}).get('spec') or {}) if document.get('kind') != 'Pod' else spec
        for container in (pod_spec.get('containers') or []) + (pod_spec.get('initContainers') or []):
            for env in container.get('env') or []:
                if any(key in env.get('name', '').lower() for key in
                       ('password', 'passwd', 'secret', 'token', 'key', 'url', 'credential', 'auth')):
                    remember(env.get('value'))
                reference = (env.get('valueFrom') or {}).get('secretKeyRef')
                if reference:
                    secret = secrets_by_name.get(reference.get('name'))
                    unresolved |= secret is None or secret.get(reference.get('key')) is None
            for env in container.get('envFrom') or []:
                reference = env.get('secretRef')
                if reference:
                    secret = secrets_by_name.get(reference.get('name'))
                    unresolved |= secret is None or any(value is None for value in secret.values())
        for volume in pod_spec.get('volumes') or []:
            references = []
            if volume.get('secret'):
                references.append(volume['secret'].get('secretName'))
            for source in (volume.get('projected') or {}).get('sources') or []:
                if source.get('secret'):
                    references.append(source['secret'].get('name'))
            for name in references:
                secret = secrets_by_name.get(name)
                unresolved |= secret is None or any(value is None for value in secret.values())
    return tuple(sorted(values, key=len, reverse=True)), unresolved


def redact(text: str, secret_values: tuple[str, ...] = ()) -> str:
    values = sorted({value for value in secret_values if value}, key=len, reverse=True)
    if values:
        text = re.sub('|'.join(re.escape(value) for value in values), '[REDACTED]', text)
    text = re.sub(r'(?i)(https?://|redis://|postgres(?:ql)?://|mysql://)([^\s/@]+)@',
                  r'\1[REDACTED]@', text)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*(?:bearer\s+|basic\s+)?)[^\s,;]+',
                  r'\1[REDACTED]', text)
    text = re.sub(r'''(?ix)((?:password|passwd|token|api[_-]?key|secret)\s*["']?\s*[:=]\s*)
                     (?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)''', r'\1[REDACTED]', text)
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
                  '[REDACTED PRIVATE KEY]', text, flags=re.S)
    return text[-MAX_TEXT:]


def collect_pod_diagnostics(namespace: str, deployment: str, secret_values: tuple[str, ...] = (),
                            include_text: bool = True) -> dict:
    report = {'pods': [], 'collected_at': datetime.now(timezone.utc).isoformat(),
              'collection_errors': [], 'truncated': False}
    if not include_text:
        report['collection_errors'].append('secret_redaction_unresolved_text_withheld')
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
            logs = execute(*base, '--previous', optional=True) if include_text and state.get('restartCount', 0) else None
            container['logs_source'] = 'previous' if logs is not None else 'current'
            if logs is None and include_text:
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
            'message': redact(str(event.get('message', '')), secret_values)[-1000:] if include_text else '[WITHHELD]',
            'count': event.get('count')} for event in events[-MAX_EVENTS:]]
        report['pods'].append(entry)
    return report
