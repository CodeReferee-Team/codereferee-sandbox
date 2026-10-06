#!/usr/bin/env python3
"""Observe a Kubernetes Deployment rollout restart and its recovery."""
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
    target = target_from(args)
    source_pod = get_ready_pod(target["namespace"], target["labelSelector"], target["deployment"])
    initial_pod_uids = {pod["uid"] for pod in ready_pods(target)}
    configuration = get_target_configuration(target["namespace"], target["deployment"])
    started_at = now_utc()
    with InClusterProbe(target['namespace'], target['service'], target['servicePort'],
                        local_port=args.local_port, timeout=args.request_timeout_seconds) as observer:
        baseline = {'probes': observer.collect(args.baseline_probes)}
        if not all(p['success'] for p in baseline['probes']):
            raise RuntimeError('Rollout baseline is unhealthy; restart skipped.')
        fault_started = time.monotonic()
        started_at = now_utc()
        kubectl(target["namespace"], "rollout", "restart", f"deployment/{target['deployment']}")
        fault_probes = []
        recovered = False
        replacement = {}
        deadline = time.monotonic() + args.recovery_timeout_seconds
        consecutive = 0
        while time.monotonic() < deadline:
            probe = observer.probe()
            fault_probes.append(probe)
            pods = ready_pods(target)
            replaced = len(pods) >= configuration['replicas'] and all(p['uid'] not in initial_pod_uids for p in pods)
            consecutive = consecutive + 1 if replaced and probe['success'] else 0
            if consecutive >= args.recovery_probes:
                replacement = pods[0]
                recovered = True
                break
            time.sleep(0.5)
        recovery_probes = {'probes': []}

    probes = [*baseline["probes"], *fault_probes, *recovery_probes["probes"]]
    denominator = 'all measured in-cluster HTTP requests during baseline, rollout restart, and replacement verification'
    baseline_metrics = metrics_from_probes(baseline["probes"])
    baseline_metrics["error_rate_denominator"] = "all HTTP GET / probes collected during baseline; readiness wait probes are excluded"
    observed_metrics = metrics_from_probes(probes)
    observed_metrics["error_rate_denominator"] = denominator
    output = {
        "schemaVersion": "chaos-v1", "scenario": "rollout_restart", "observationStatus": "observed",
        'exitCode': 0 if recovered else 1, 'timedOut': not recovered,
        'probeTransport': 'in_cluster_http',
        'probes': {'baseline': baseline['probes'], 'experiment': fault_probes},
        "target": target, "replicas": configuration["replicas"], "baseline": {"metrics": baseline_metrics},
        "metrics": observed_metrics,
        "chaos_observation": {
            "type": "rollout_restart", "kill_method": "kubectl_rollout_restart", "started_at": started_at,
            'recovered': recovered,
            "recovered_at": now_utc() if recovered else None,
            "recovery_seconds": measured_recovery_seconds(fault_probes),
            'experiment_duration_seconds': round(time.monotonic() - fault_started, 2),
            "target_pod_uid": source_pod["uid"], "replacement_pod_uid": replacement.get('uid'),
            "target_configuration": configuration,
            "rollout_operation": {"deployment": target["deployment"], "strategy": "kubectl rollout restart"},
            "observation_window": {"baseline_probe_count": args.baseline_probes,
                                   "recovery_timeout_seconds": args.recovery_timeout_seconds,
                                   "error_rate_denominator": denominator},
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
    parser = argparse.ArgumentParser(description="Run a Deployment rollout restart chaos observation.")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--service-port", type=int, required=True)
    parser.add_argument("--label-selector", required=True)
    parser.add_argument("--baseline-probes", type=int, default=10)
    parser.add_argument("--recovery-probes", type=int, default=5)
    parser.add_argument("--request-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--recovery-timeout-seconds", type=int, default=180)
    parser.add_argument("--local-port", type=int, default=18083)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def target_from(args: argparse.Namespace) -> dict[str, Any]:
    return {"namespace": args.namespace, "deployment": args.deployment, "service": args.service,
            "servicePort": args.service_port, "labelSelector": args.label_selector}


def kubectl(namespace: str, *args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(kubectl_command("-n", namespace, *args), text=True, capture_output=True, env=kubectl_environment())
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "kubectl command failed")
    return completed


def ready_pods(target: dict[str, Any]) -> list[dict[str, Any]]:
    payload = json.loads(kubectl(target["namespace"], "get", "pods", "-l", target["labelSelector"], "-o", "json").stdout)
    return [
        {"name": item["metadata"]["name"], "uid": item["metadata"]["uid"]}
        for item in payload.get("items", [])
        if item.get("metadata", {}).get("deletionTimestamp") is None
        and any(condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in item.get("status", {}).get("conditions", []))
    ]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
