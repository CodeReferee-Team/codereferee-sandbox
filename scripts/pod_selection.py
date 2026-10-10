"""Select bounded, Deployment-owned Pods without percentage rounding."""
from __future__ import annotations

from typing import Any


MAX_TARGET_PODS = 16


def validate_count(count: int) -> int:
    if type(count) is not int or not 1 <= count <= MAX_TARGET_PODS:
        raise ValueError(f'podsAffectedCount must be an integer in 1..{MAX_TARGET_PODS}.')
    return count


def controlled_by(resource: dict, kind: str, uid: str) -> bool:
    return bool(uid) and any(owner.get('kind') == kind and owner.get('uid') == uid
                            and owner.get('controller') is True
                            for owner in resource.get('metadata', {}).get('ownerReferences', []))


def owned_pods(deployment: dict, replica_sets: list[dict], pods: list[dict]) -> list[dict]:
    metadata = deployment.get('metadata', {})
    namespace, uid = metadata.get('namespace'), metadata.get('uid')
    if not namespace or not uid:
        raise RuntimeError('Deployment namespace/UID missing; cannot verify fault ownership.')
    rs_uids = {item['metadata']['uid'] for item in replica_sets
               if item.get('metadata', {}).get('namespace') == namespace
               and item.get('metadata', {}).get('uid')
               and controlled_by(item, 'Deployment', uid)}
    return [pod for pod in pods if pod.get('metadata', {}).get('namespace') == namespace
            and any(controlled_by(pod, 'ReplicaSet', rs_uid) for rs_uid in rs_uids)]


def select_ready_pods(pods: list[dict], count: int, container: str) -> list[dict]:
    validate_count(count)
    ready = []
    for pod in pods:
        metadata, status = pod.get('metadata', {}), pod.get('status', {})
        if metadata.get('deletionTimestamp') or status.get('phase') != 'Running':
            continue
        if not metadata.get('name') or not metadata.get('uid'):
            continue
        if not any(c.get('type') == 'Ready' and c.get('status') == 'True'
                   for c in status.get('conditions', [])):
            continue
        if not any(c.get('name') == container and c.get('ready') is True
                   for c in status.get('containerStatuses', [])):
            continue
        ready.append(pod)
    ready.sort(key=lambda pod: pod['metadata']['name'])
    if len(ready) < count:
        raise RuntimeError(f'Requested {count} fault targets but only {len(ready)} owned Ready Pods exist; '
                           'injection skipped rather than broadening the scope.')
    return ready[:count]


def pod_evidence(pod: dict[str, Any], container: str) -> dict[str, Any]:
    metadata = pod.get('metadata', {})
    status = next((item for item in pod.get('status', {}).get('containerStatuses', [])
                   if item.get('name') == container), {})
    return {'name': metadata.get('name'), 'uid': metadata.get('uid'),
            'node': pod.get('spec', {}).get('nodeName'),
            'restart_count': status.get('restartCount'), 'container_id': status.get('containerID'),
            'ready': status.get('ready'), 'deletion_timestamp': metadata.get('deletionTimestamp')}
