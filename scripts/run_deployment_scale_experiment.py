#!/usr/bin/env python3
"""Inject a Kubernetes API scale-down fault, then restore the Deployment."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collect_baseline import kubectl_command, kubectl_environment
from in_cluster_probe import InClusterProbe
from run_litmus_pod_delete import measured_recovery_seconds
from run_pod_kill_experiment import (
    get_ready_pod,
    get_target_configuration,
    metrics_from_probes,
)


def main() -> int:
    args = parse_args()
    target = load_target(args)
    source_pod = get_ready_pod(target["namespace"], target["labelSelector"], target["deployment"])
    configuration = get_target_configuration(target["namespace"], target["deployment"])
    original_replicas = configuration["replicas"]
    if original_replicas < 1:
        raise SystemExit("Deployment must have at least one replica before scale-down chaos.")

    started_at = now_utc()
    with InClusterProbe(target['namespace'], target['service'], int(target['servicePort']),
                        local_port=args.local_port, timeout=args.request_timeout_seconds) as observer:
        baseline = {'probes': observer.collect(args.baseline_probes)}
        if not all(p['success'] for p in baseline['probes']):
            raise RuntimeError('Scale-down baseline is unhealthy; fault skipped.')
        try:
            fault_started = time.monotonic()
            started_at = now_utc()
            scale(target['namespace'], target['deployment'], 0)
            fault_probes = []
            deadline = time.monotonic() + args.fault_seconds
            while time.monotonic() < deadline:
                fault_probes.append(observer.probe())
                time.sleep(0.5)
            scale(target['namespace'], target['deployment'], original_replicas)
            recovered_probes, recovered = observer.collect_until_healthy(
                args.recovery_timeout_seconds, args.recovery_probes)
            recovery_probes = {'probes': recovered_probes}
            replacement_pod = get_ready_pod(target['namespace'], target['labelSelector'],
                                            target['deployment']) if recovered else {}
        finally:
            scale(target['namespace'], target['deployment'], original_replicas)

    probes = [*baseline["probes"], *fault_probes, *recovery_probes["probes"]]
    denominator = 'all measured in-cluster HTTP requests during baseline, scale-down, and recovery'
    baseline_metrics = metrics_from_probes(baseline["probes"])
    baseline_metrics["error_rate_denominator"] = "all HTTP GET / probes collected during baseline; readiness wait probes are excluded"
    observed_metrics = metrics_from_probes(probes)
    observed_metrics["error_rate_denominator"] = denominator
    output = {
        "schemaVersion": "chaos-v1",
        "scenario": "deployment_scale_down",
        "observationStatus": "observed",
        'exitCode': 0 if recovered else 1,
        'timedOut': not recovered,
        'probeTransport': 'in_cluster_http',
        'probes': {'baseline': baseline['probes'], 'experiment': [*fault_probes, *recovery_probes['probes']]},
        "target": target,
        "replicas": original_replicas,
        "baseline": {"metrics": baseline_metrics},
        "metrics": observed_metrics,
        "chaos_observation": {
            "type": "deployment_scale_down",
            "kill_method": "kubectl_scale",
            "started_at": started_at,
            'recovered': recovered,
            "recovered_at": now_utc() if recovered else None,
            "recovery_seconds": measured_recovery_seconds([*fault_probes, *recovery_probes['probes']]),
            'experiment_duration_seconds': round(time.monotonic() - fault_started, 2),
            "target_pod_uid": source_pod.get("uid"),
            "replacement_pod_uid": replacement_pod.get("uid"),
            "target_configuration": configuration,
            "scale_operation": {"from_replicas": original_replicas, "to_replicas": 0, "restored_replicas": original_replicas},
            "observation_window": {
                "baseline_probe_count": args.baseline_probes,
                "fault_seconds": args.fault_seconds,
                "recovery_timeout_seconds": args.recovery_timeout_seconds,
                "error_rate_denominator": denominator,
            },
            "abort_condition": {"triggered": False, "reason": None},
        },
        'source': {'real_execution_observed': True, 'target': target['deployment']},
    }
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if recovered else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Deployment scale-down and restore chaos.")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--service-port", type=int, required=True)
    parser.add_argument("--label-selector", required=True)
    parser.add_argument("--baseline-probes", type=int, default=10)
    parser.add_argument("--recovery-probes", type=int, default=5)
    parser.add_argument("--fault-seconds", type=int, default=10)
    parser.add_argument("--request-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--recovery-timeout-seconds", type=int, default=180)
    parser.add_argument("--local-port", type=int, default=18081)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def load_target(args: argparse.Namespace) -> dict[str, Any]:
    return {"namespace": args.namespace, "deployment": args.deployment, "service": args.service,
            "servicePort": args.service_port, "labelSelector": args.label_selector}


def scale(namespace: str, deployment: str, replicas: int) -> None:
    completed = subprocess.run(kubectl_command("scale", f"deployment/{deployment}", "-n", namespace, f"--replicas={replicas}"),
                               text=True, capture_output=True, env=kubectl_environment())
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "kubectl scale failed")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
