"""Create/reuse the local persistent receiver without touching BE Prometheus.

Never removes volumes or replaces an incompatible existing named container.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]
NAME = 'codereferee-metrics-receiver'
QUERY_URL = 'http://127.0.0.1:19091'
WRITE_URL = 'http://codereferee-metrics-receiver:9090/api/v1/write'


def run(arguments, *, check=True):
    result = subprocess.run(['docker', *arguments], text=True, capture_output=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError('Receiver setup command failed: ' + result.stderr[-1200:])
    return result


def validate_receiver(container):
    if '--web.enable-remote-write-receiver' not in container.get('Config', {}).get('Cmd', []):
        raise RuntimeError('Existing named container is not a remote-write receiver; preserving it.')
    if 'kind' not in container.get('NetworkSettings', {}).get('Networks', {}):
        raise RuntimeError('Existing receiver is not attached to kind; preserving it.')
    ports = container.get('HostConfig', {}).get('PortBindings', {}).get('9090/tcp') or []
    if not any(p.get('HostIp') == '127.0.0.1' and p.get('HostPort') == '19091' for p in ports):
        raise RuntimeError('Existing receiver must bind only the expected localhost port; preserving it.')
    if not any(m.get('Type') == 'volume' and m.get('Destination') == '/prometheus' for m in container.get('Mounts', [])):
        raise RuntimeError('Receiver data must use a persistent Docker volume; preserving it.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shell', choices=('powershell', 'bash'), default='powershell' if os.name == 'nt' else 'bash')
    args = parser.parse_args()
    run(['info'])
    run(['network', 'inspect', 'kind'])
    inspected = run(['inspect', NAME], check=False)
    if inspected.returncode:
        if 'no such' not in inspected.stderr.lower():
            raise RuntimeError('Could not safely inspect existing receiver.')
        run(['compose', '-f', str(ROOT / 'infra' / 'metrics-receiver' / 'compose.yaml'), 'up', '-d', 'receiver'])
        inspected = run(['inspect', NAME])
    container = json.loads(inspected.stdout)[0]
    validate_receiver(container)
    if not container['State']['Running']:
        run(['start', NAME])
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 45
    while True:
        try:
            with opener.open(QUERY_URL + '/-/ready', timeout=3) as response:
                if response.status == 200:
                    break
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError('Receiver did not become ready.')
            time.sleep(1)
    print('Persistent receiver Ready; existing data volumes were not removed.')
    for key, value in [('CODEREFEREE_METRICS_REMOTE_WRITE_URL', WRITE_URL), ('CODEREFEREE_METRICS_QUERY_URL', QUERY_URL)]:
        print(f"$env:{key}='{value}'" if args.shell == 'powershell' else f"export {key}='{value}'")
    print('Restart Sandbox API in the configured shell. Receiver survives request cleanup.')


if __name__ == '__main__':
    main()
