"""Remove only the exact image belonging to a completed request.

No global prune and no force removal. Namespace deletion must precede node cleanup.
"""
import os
import re
import subprocess
import time

try:
    from .collect_baseline import kubectl_environment
except ImportError:
    from collect_baseline import kubectl_environment


def cleanup_request_image(image: str, namespace: str, *, remove_from_kind: bool = True,
                          deadline: float | None = None) -> dict:
    match = re.fullmatch(r'codereferee/([a-z0-9-]+)(?:/svc-[a-z][a-z0-9-]{0,30})?:[a-f0-9]{12}', image)
    if not match or namespace != 'codereferee-' + match[1]:
        raise ValueError('Image does not belong to the supplied request namespace.')
    report = {'image': image, 'host_removed': False, 'kind_nodes': [], 'errors': []}
    environment = kubectl_environment()
    def limit(seconds):
        if deadline is None:
            return seconds
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Image cleanup budget exhausted.')
        return min(seconds, remaining)
    if remove_from_kind and os.getenv('CODEREFEREE_CLUSTER_PROVIDER') == 'kind':
        nodes = subprocess.run([os.getenv('CODEREFEREE_KIND_COMMAND', 'kind'), 'get', 'nodes',
            '--name', os.getenv('CODEREFEREE_KIND_CLUSTER_NAME', 'codereferee')],
            capture_output=True, text=True, env=environment, timeout=limit(20))
        if nodes.returncode:
            report['errors'].append(nodes.stderr.strip())
        else:
            for node in nodes.stdout.splitlines():
                removed = subprocess.run(['docker', 'exec', node, 'crictl', 'rmi', image],
                    capture_output=True, text=True, env=environment, timeout=limit(30))
                # Multiple service tags may point at one image ID. Removing the first
                # can remove its aliases too; already absent is the desired end state.
                absent = removed.returncode != 0 and 'no such image ' in removed.stderr.lower()
                row = {'node': node, 'removed': removed.returncode == 0 or absent}
                if absent:
                    row['already_absent'] = True
                report['kind_nodes'].append(row)
                if removed.returncode and not absent:
                    report['errors'].append(removed.stderr.strip())
    removed = subprocess.run(['docker', 'image', 'rm', image], capture_output=True,
                             text=True, env=environment, timeout=limit(30))
    absent = removed.returncode != 0 and 'no such image:' in removed.stderr.lower()
    report['host_removed'] = removed.returncode == 0 or absent
    if absent:
        report['host_already_absent'] = True
    if removed.returncode and not absent:
        report['errors'].append(removed.stderr.strip())
    return report
