"""Observe a deployed service without injecting Chaos or inventing test coverage."""
import argparse
import json
import time

from in_cluster_probe import InClusterProbe
from run_pod_kill_experiment import metrics_from_probes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--service-port', required=True, type=int)
    parser.add_argument('--probe-path', default='/')
    args = parser.parse_args()
    started = time.monotonic()
    with InClusterProbe(args.namespace, args.service, args.service_port, path=args.probe_path) as observer:
        probes = observer.collect(5)
    success = all(p['success'] for p in probes)
    result = {'schemaVersion': 'sandbox-result.v1', 'observationStatus': 'observed',
        'exitCode': 0 if success else 1, 'timedOut': False,
        'durationMillis': round((time.monotonic() - started) * 1000),
        'serverStarted': any(p['success'] for p in probes),
        'serverUrl': f'http://{args.service}.{args.namespace}.svc.cluster.local:{args.service_port}',
        'httpStatus': probes[-1]['status_code'], 'serviceCheckAttempted': True,
        'browserCheckAttempted': False, 'browserLoaded': False,
        'probeTransport': 'in_cluster_http', 'metrics': metrics_from_probes(probes),
        'stdout': 'Repository built, deployed, and HTTP smoke observed.' if success else '',
        'stderr': '' if success else 'Service HTTP smoke failed.',
        'sandboxReport': {'schema_version': 'sandbox-result.v1', 'outcome': 'passed' if success else 'failed',
                         'failed_step': 'none' if success else 'smoke',
                         'verification_declared': False,
                         'test_execution': 'not_attempted', 'service_smoke_observed': True},
        'source': {'real_execution_observed': True, 'namespace': args.namespace,
                   'probes': probes, 'chaos_injected': False}}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
