"""Remove only the exact image belonging to a completed request.

No global prune and no force removal. Namespace deletion must precede node cleanup.
"""
import os
import re
import subprocess

try:
    from .collect_baseline import kubectl_environment
except ImportError:
    from collect_baseline import kubectl_environment


def cleanup_request_image(image: str, namespace: str, *, remove_from_kind: bool = True) -> dict:
    match = re.fullmatch(r'codereferee/([a-z0-9-]+):[a-f0-9]{12}', image)
    if not match or namespace != 'codereferee-' + match[1]:
        raise ValueError('Image does not belong to the supplied request namespace.')
    report = {'image': image, 'host_removed': False, 'kind_nodes': [], 'errors': []}
    environment = kubectl_environment()
    if remove_from_kind and os.getenv('CODEREFEREE_CLUSTER_PROVIDER') == 'kind':
        nodes = subprocess.run([os.getenv('CODEREFEREE_KIND_COMMAND', 'kind'), 'get', 'nodes',
            '--name', os.getenv('CODEREFEREE_KIND_CLUSTER_NAME', 'codereferee')],
            capture_output=True, text=True, env=environment, timeout=20)
        if nodes.returncode:
            report['errors'].append(nodes.stderr.strip())
        else:
            for node in nodes.stdout.splitlines():
                removed = subprocess.run(['docker', 'exec', node, 'crictl', 'rmi', image],
                    capture_output=True, text=True, env=environment, timeout=30)
                report['kind_nodes'].append({'node': node, 'removed': removed.returncode == 0})
                if removed.returncode:
                    report['errors'].append(removed.stderr.strip())
    removed = subprocess.run(['docker', 'image', 'rm', image], capture_output=True,
                             text=True, env=environment, timeout=30)
    report['host_removed'] = removed.returncode == 0
    if removed.returncode:
        report['errors'].append(removed.stderr.strip())
    return report
