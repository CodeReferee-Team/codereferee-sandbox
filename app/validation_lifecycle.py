"""Request replay, bounded preparation retries and recovery of interrupted work."""
from __future__ import annotations

import json
import time
from uuid import uuid4

from fastapi import HTTPException

from app.executions import ExecutionSlot, ExecutionStore, execution_context
from scripts.request_recovery import create_inventory, recover_inventory


RETRY_PHASES = {'clone', 'install', 'build', 'test', 'image_build', 'image_load'}
TRANSIENT_ERRORS = ('Could not resolve host', 'Failed to connect to', 'RemoteDisconnected',
    'Connection reset', 'Temporary failure in name resolution', 'EAI_AGAIN', 'ECONNRESET',
    '429 Too Many Requests', '503 Server Error')


def failure(request_id, reason, code):
    return {'schemaVersion': 'chaos-v1', 'observationStatus': 'infrastructure_error',
        'exitCode': None, 'timedOut': False, 'infraError': code, 'requestId': request_id,
        'stderr': reason, 'serverStarted': False, 'serviceCheckAttempted': False,
        'browserCheckAttempted': False, 'source': {}}


def replay(row):
    result = json.loads(row['result'])
    result.setdefault('source', {}).setdefault('execution', {}).update(
        executionId=row['execution_id'], state=row['state'], replayed=True)
    return result


def recover_previous(store):
    for row in store.unfinished():
        result = json.loads(row['result']) if row['result'] else failure(row['request_id'],
            'Sandbox execution was interrupted before a final result was stored.', 'sandbox_worker_interrupted')
        cleanup = {'complete': True}
        if row['resource_id']:
            path = store.directory / (row['resource_id'] + '.json')
            if path.exists():
                cleanup = recover_inventory(path)
            else:
                cleanup = {'complete': False, 'errors': ['Recovery inventory is missing.']}
        result.setdefault('source', {})['execution_recovery'] = cleanup
        result['source']['execution_cleanup'] = cleanup
        state = 'completed' if cleanup['complete'] else 'cleanup_pending'
        store.finish(row['execution_id'], state, result)
        if not cleanup['complete']:
            raise HTTPException(status_code=503, detail='Previous execution cleanup is pending.',
                                headers={'Retry-After': '5'})


def retry_reason(result, phase):
    if result.get('observationStatus') != 'infrastructure_error' or phase not in RETRY_PHASES:
        return None
    if result.get('timedOut'):
        return 'preparation_timeout'
    text = result.get('stderr', '')
    steps = result.get('sandboxReport', {}).get('steps', [])
    text += '\n'.join(step.get('stderr', '') + step.get('stdout', '') for step in steps)
    if any(term in text for term in TRANSIENT_ERRORS):
        return 'transient_preparation_error'
    return None


def reported_cleanup_complete(result):
    def check_verification(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'check_cache_cleanup', 'check_container_cleanup', 'check_setup_container_cleanup',
                           'check_database_cleanup'} and isinstance(item, dict) and not item.get('removed'):
                    return False
                if not check_verification(item):
                    return False
        elif isinstance(value, list):
            return all(check_verification(item) for item in value)
        return True
    source = result.get('source', {})
    cleanup = source.get('artifact_cleanup', {})
    metrics = source.get('metrics_observation', {})
    if metrics.get('cleanup', {}).get('attempted') and not metrics['cleanup'].get('removed'):
        return False
    workspace = source.get('workspace_cleanup', cleanup.get('workspace_cleanup', {}))
    if not cleanup.get('namespace_removed') or cleanup.get('errors') or not workspace.get('removed'):
        return False
    images = cleanup.get('images', [cleanup] if 'image' in cleanup else [])
    return check_verification(result.get('sandboxReport', {})) and all(item.get('host_removed') and not item.get('errors') and
        all(node.get('removed') for node in item.get('kind_nodes', [])) for item in images)


def submit(request, handler):
    if request.preparation_retries and request.deadline_seconds is None:
        raise HTTPException(status_code=422, detail='Preparation retries require deadlineSeconds.')
    if request.request_id is None:
        request = request.model_copy(update={'request_id': str(uuid4())})
    store = ExecutionStore()
    identity = store.identity(request)
    existing = store.get(identity)
    if existing and existing['state'] == 'completed':
        return replay(existing)
    slot = ExecutionSlot(store.directory)
    if not slot.acquire():
        raise HTTPException(status_code=409, detail='A Chaos v1 experiment is already running.',
                            headers={'Retry-After': '2'})
    try:
        recover_previous(store)
        existing = store.get(identity)
        if existing:
            return replay(existing)
        store.start(identity, request.request_id)
        return execute_attempts(store, identity, request, handler)
    finally:
        slot.release()


def execute_attempts(store, identity, request, handler):
    started = time.monotonic()
    history = []
    preparation_limit = request.preparation_timeout_seconds or 1200
    phase_limits = dict(request.phase_timeouts)
    commit = request.commit_sha
    result = None
    cleanup = {'complete': True}
    stopped_reason = None
    for index in range(request.preparation_retries + 1):
        remaining = None if request.deadline_seconds is None else request.deadline_seconds - (time.monotonic()-started)
        if index and remaining <= 91:
            stopped_reason = 'total_budget_insufficient'
            break
        resource_id = 'exec-' + uuid4().hex[:24]
        journal = create_inventory(store.directory, resource_id)
        store.attempt(identity, resource_id)
        attempt = request.model_copy(update={'commit_sha': commit,
            'preparation_timeout_seconds': preparation_limit, 'phase_timeouts': dict(phase_limits),
            'deadline_seconds': int(remaining) if remaining is not None else None})
        token = execution_context.set({'journal': journal, 'resource_id': resource_id,
            'deadline': started + request.deadline_seconds if request.deadline_seconds is not None else None})
        exception = None
        try:
            result = handler(attempt)
        except Exception as exc:
            exception = exc
            result = failure(request.request_id, 'Sandbox execution ended unexpectedly.', 'sandbox_execution_interrupted')
        finally:
            execution_context.reset(token)
        data = json.loads(journal.read_text(encoding='utf-8'))
        phase = data.get('phase', 'starting')
        cleanup = {'complete': True}
        if not reported_cleanup_complete(result):
            cleanup_limit = 90 if request.deadline_seconds is None else max(0,
                request.deadline_seconds - (time.monotonic()-started))
            cleanup = recover_inventory(journal, timeout_seconds=cleanup_limit)
        result['requestId'] = request.request_id
        result.setdefault('source', {})['execution_cleanup'] = cleanup
        reason = retry_reason(result, phase)
        history.append({'attempt': index+1, 'resourceId': resource_id, 'phase': phase,
            'preparationLimitSeconds': preparation_limit, 'phaseLimits': dict(phase_limits),
            'observationStatus': result.get('observationStatus'), 'exitCode': result.get('exitCode'),
            'timedOut': bool(result.get('timedOut')), 'retryReason': reason,
            'cleanupComplete': cleanup['complete'], 'elapsedSeconds': round(time.monotonic()-started, 3),
            'failureEvidence': {key: result[key] for key in ('failurePhase', 'infraError', 'stderr',
                'budget', 'sandboxReport') if key in result} if result.get('exitCode') != 0 else None})
        if exception or not reason or not cleanup['complete'] or index == request.preparation_retries:
            if not cleanup['complete']:
                stopped_reason = 'cleanup_pending'
            elif reason and index == request.preparation_retries:
                stopped_reason = 'attempt_limit'
            break
        if reason == 'preparation_timeout':
            preparation_limit = min(7200, preparation_limit * 2)
            phase_limits[phase] = min(7200, phase_limits.get(phase, 600) * 2)
        commit = (data.get('commits') or [commit])[0]
    result.setdefault('source', {})['execution'] = {'executionId': identity, 'replayed': False,
        'state': 'completed' if cleanup['complete'] else 'cleanup_pending', 'attempts': history,
        'totalLimitSeconds': request.deadline_seconds, 'retryStoppedReason': stopped_reason,
        'elapsedSeconds': round(time.monotonic()-started, 3)}
    store.finish(identity, result['source']['execution']['state'], result)
    if exception:
        if isinstance(exception, HTTPException) and cleanup['complete']:
            store.discard(identity)
        raise exception
    return result
