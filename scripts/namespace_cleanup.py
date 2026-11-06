"""Confirm namespace absence before reclaiming request images; never force finalizers."""
import re
import subprocess
import time

try:
    from .collect_baseline import kubectl_command, kubectl_environment
except ImportError:
    from collect_baseline import kubectl_command, kubectl_environment


def delete_namespace_confirmed(namespace: str, *, timeout_seconds: float = 120) -> bool:
    if len(namespace) > 63 or not re.fullmatch(r'codereferee-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', namespace):
        return False
    deadline = time.monotonic() + timeout_seconds
    environment = kubectl_environment()

    def invoke(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Namespace cleanup budget exhausted')
        return subprocess.run(kubectl_command(*args, '--request-timeout=10s'),
            text=True, capture_output=True, env=environment, timeout=min(15, remaining))

    try:
        deleted = invoke('delete', 'namespace', namespace, '--ignore-not-found=true', '--wait=false')
        if deleted.returncode:
            return False
        while time.monotonic() < deadline:
            current = invoke('get', 'namespace', namespace, '--ignore-not-found=true', '-o', 'json')
            # An API failure or a Terminating object is not proof of deletion.
            if current.returncode == 0 and not current.stdout.strip():
                return True
            time.sleep(min(1, max(0, deadline-time.monotonic())))
    except (OSError, subprocess.TimeoutExpired, TimeoutError):
        return False
    return False
