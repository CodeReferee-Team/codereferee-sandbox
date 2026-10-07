"""Disconnect a declared dependency Pod, observe a business API, then recover.

Litmus netem packet loss preserves the dependency Pod and its data. Only a
dependency declared by the deployment profile is targeted.
"""
import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from collect_baseline import kubectl_command, kubectl_environment, get_ready_pod
from in_cluster_probe import InClusterProbe
from run_pod_kill_experiment import get_target_configuration, metrics_from_probes
from run_litmus_pod_delete import measured_recovery_seconds, apply, wait_for_result, classify_result


def kubectl(namespace, *args):
    result = subprocess.run(kubectl_command('-n', namespace, *args), capture_output=True,
                            text=True, timeout=30, env=kubectl_environment())
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--deployment', required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--service-port', type=int, required=True)
    parser.add_argument('--dependency-config', required=True)
    parser.add_argument('--scenario', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    config = json.loads(args.dependency_config)
    if not args.namespace.startswith('codereferee-'):
        raise RuntimeError('Dependency faults require a CodeReferee Sandbox namespace.')
    pod = get_ready_pod(args.namespace, config['labelSelector'], config['deployment'])
    context = {}
    if config.get('contextFromDeploymentEnv'):
        deployment = json.loads(kubectl(args.namespace, 'get', 'deployment', args.deployment, '-o', 'json').stdout)
        env = {item['name']: item.get('value') for item in deployment['spec']['template']['spec']['containers'][0].get('env', [])}
        context = {key: env[name] for key, name in config['contextFromDeploymentEnv'].items()}
        if any(value is None for value in context.values()):
            raise RuntimeError('Required Sandbox-generated probe credentials are unavailable.')
    workload = get_target_configuration(args.namespace, args.deployment)
    engine = f'codereferee-dependency-{args.scenario.split("_")[1]}-{int(time.time())}'
    probes = []
    with InClusterProbe(args.namespace, args.service, args.service_port, local_port=18083,
                        path=config.get('probePath', '/'), spec=config.get('probeSpec'), context=context) as observer:
        baseline = observer.collect(5)
        if not all(p['success'] for p in baseline):
            raise RuntimeError('Business probe baseline failed; dependency fault was skipped.')
        started_at = datetime.now(timezone.utc).isoformat()
        apply(args.namespace, engine, config['deployment'], config['labelSelector'], 'pod_network_loss',
              config['container'], 15, overrides={'NETWORK_PACKET_LOSS_PERCENTAGE': '100'})
        result, probes = wait_for_result(args.namespace, engine, 240, observer.local_port, 2, observer=observer)
        reverted_at = datetime.now(timezone.utc).isoformat()
        deadline = time.monotonic() + 120
        successes = 0
        while time.monotonic() < deadline and successes < 5:
            probe = observer.probe()
            probes.append(probe)
            successes = successes + 1 if probe['success'] else 0
            time.sleep(0.5)
        recovered = successes >= 5
        recovered_at = datetime.now(timezone.utc).isoformat() if recovered else None
    observation_status = classify_result(result)
    output = {'schemaVersion': 'chaos-v1', 'scenario': args.scenario,
        'observationStatus': observation_status,
        'exitCode': (0 if recovered else 1) if observation_status == 'observed' else None,
        'infraError': 'dependency_fault_injection_failed' if observation_status != 'observed' else None,
        'timedOut': not recovered, 'chaosResult': result,
        'probeTransport': 'in_cluster_business_http',
        'baseline': {'metrics': metrics_from_probes(baseline)},
        'metrics': metrics_from_probes([*baseline, *probes]),
        'probes': {'baseline': baseline, 'experiment': probes},
        'chaos_observation': {'type': args.scenario, 'kill_method': 'litmus_dependency_network_loss',
            'target_pod_uid': pod['uid'], 'target_configuration': workload,
            'dependency': {'deployment': config['deployment'], 'container': config['container'],
                           'fault_parameters': {'packet_loss_percent': 100, 'duration_seconds': 15}},
            'started_at': started_at, 'fault_reverted_at': reverted_at, 'recovered_at': recovered_at,
            'recovered': recovered, 'recovery_seconds': measured_recovery_seconds(probes) if recovered else None,
            'business_probe': {'path': config.get('probePath'), 'steps': [s['path'] for s in config.get('probeSpec', {}).get('steps', [])]},
            'observation_window': {'fault_seconds': 15, 'recovery_timeout_seconds': 120},
            'abort_condition': {'triggered': False, 'reason': None}},
        'source': {'real_execution_observed': True}}
    output['metrics']['error_rate_denominator'] = 'all measured business HTTP workflow attempts; setup and readiness excluded'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(output))
    return 0 if output['exitCode'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
