"""Provision the optional Chromium runtime before enabling browserProbe."""
import argparse
import re
import shutil
import subprocess
from pathlib import Path

from browser_probe import BROWSER_IMAGE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind-cluster', help='Load the runtime and browser seccomp profile into every kind node.')
    args = parser.parse_args()
    if args.kind_cluster and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', args.kind_cluster):
        parser.error('kind cluster name must be a lowercase DNS label.')
    root = Path(__file__).resolve().parents[1]
    if shutil.disk_usage(root).free < 5 * 1024 ** 3:
        raise SystemExit('Browser runtime build requires at least 5 GiB free on the workspace drive.')
    subprocess.run(['docker', 'build', '--tag', BROWSER_IMAGE, '--file', str(root / 'browser/Dockerfile'),
                    str(root / 'browser')], check=True, timeout=900)
    if args.kind_cluster:
        import os
        kind = os.getenv('CODEREFEREE_KIND_COMMAND', 'kind')
        subprocess.run([kind, 'load', 'docker-image', BROWSER_IMAGE, '--name', args.kind_cluster],
                       check=True, timeout=900)
        nodes = subprocess.run([kind, 'get', 'nodes', '--name', args.kind_cluster],
                               capture_output=True, text=True, check=True, timeout=30).stdout.splitlines()
        if not nodes:
            raise SystemExit('The selected kind cluster has no nodes.')
        for node in nodes:
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', node):
                raise SystemExit('kind returned an invalid node name.')
            subprocess.run(['docker', 'exec', node, 'mkdir', '-p', '/var/lib/kubelet/seccomp'],
                           check=True, timeout=30)
            subprocess.run(['docker', 'cp', str(root / 'browser/seccomp.json'),
                            node + ':/var/lib/kubelet/seccomp/codereferee-browser.json'], check=True, timeout=30)
        print('Set CODEREFEREE_BROWSER_SECCOMP_PROFILE=codereferee-browser.json in the Sandbox API environment.')


if __name__ == '__main__':
    main()
