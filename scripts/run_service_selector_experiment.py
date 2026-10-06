#!/usr/bin/env python3
"""Inject a Kubernetes Service selector blackhole, then restore routing."""
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
from run_pod_kill_experiment import (
    collect_probes,
    get_ready_pod,
    get_target_configuration,
    metrics_from_probes,
    start_port_forward,
    stop_process,
    wait_for_service,
)


def main() -> int:
    args = parse_args()
    target = target_from(args)
    source_pod = get_ready_pod(target["namespace"], target["labelSelector"], target["deployment"])
    configuration = get_target_configuration(target["namespace"], target["deployment"])
    original_selector = service_selector(target["namespace"], target["service"])
    if not original_selector:
        raise SystemExit("Service selector is required for selector-blackhole chaos.")

    started_at = now_utc()
    with InClusterProbe(target['namespace'], target['service'], int(target['servicePort']),
                        local_port=args.local_port, timeout=args.request_timeout_seconds) as observer:
        baseline = {'probes': observer.collect(args.baseline_probes)}
        if not all(p['success'] for p in baseline['probes']):
            raise RuntimeError('Service baseline is unhealthy; routing fault skipped.')
        try:
            fault_started = time.monotonic()
            replace_selector(target["namespace"], target["service"], {"app.kubernetes.io/name": "codereferee-blackhole"})
            wait_for_endpoint_state(target["namespace"], target["service"], expected=False, timeout_seconds=args.recovery_timeout_seconds)
            fault_probes = []
            deadline = time.monotonic() + args.fault_seconds
            while time.monotonic() < deadline:
                fault_probes.append(observer.probe())
                time.sleep(0.5)
            replace_selector(target["namespace"], target["service"], original_selector)
            wait_for_endpoint_state(target["namespace"], target["service"], expected=True, timeout_seconds=args.recovery_timeout_seconds)
            recovery_probes = {'probes': observer.collect(args.recovery_probes)}
        finally:
            replace_selector(target["namespace"], target["service"], original_selector)

    probes = [*baseline["probes"], *fault_probes, *recovery_probes["probes"]]
    denominator = 'all measured in-cluster HTTP requests during baseline, routing fault, and recovery'
    recovered = all(p['success'] for p in recovery_probes['probes'])
    baseline_metrics = metrics_from_probes(baseline["probes"])
    baseline_metrics["error_rate_denominator"] = "all HTTP GET / probes collected during baseline; readiness wait probes are excluded"
    observed_metrics = metrics_from_probes(probes)
    observed_metrics["error_rate_denominator"] = denominator
    output = {
        "schemaVersion": "chaos-v1",
        "scenario": "service_selector_blackhole",
        "observationStatus": "observed",
        'exitCode': 0 if recovered else 1,
        'probeTransport': 'in_cluster_http',
        'probes': {'baseline': baseline['probes'], 'experiment': [*fault_probes, *recovery_probes['probes']]},
        "target": target,
        "replicas": configuration["replicas"],
        "baseline": {"metrics": baseline_metrics},
        "metrics": observed_metrics,
        "chaos_observation": {
            "type": "service_selector_blackhole",
            "kill_method": "kubectl_patch_service_selector",
            "started_at": started_at,
            "recovered_at": now_utc() if recovered else None,
            'recovered': recovered,
            "recovery_seconds": round(time.monotonic() - fault_started, 2),
            "target_pod_uid": source_pod.get("uid"),
            "replacement_pod_uid": None,
            "target_configuration": configuration,
            "routing_operation": {"original_selector": original_selector, "blackhole_selector": {"app.kubernetes.io/name": "codereferee-blackhole"}},
            "observation_window": {
                "baseline_probe_count": args.baseline_probes,
                "fault_seconds": args.fault_seconds,
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
    parser = argparse.ArgumentParser(description="Run Service selector blackhole and restore chaos.")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--service-port", type=int, required=True)
    parser.add_argument("--label-selector", required=True)
    parser.add_argument("--baseline-probes", type=int, default=10)
    parser.add_argument("--recovery-probes", type=int, default=5)
    parser.add_argument("--fault-seconds", type=int, default=10)
    parser.add_argument("--request-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--recovery-timeout-seconds", type=int, default=60)
    parser.add_argument("--local-port", type=int, default=18082)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def target_from(args: argparse.Namespace) -> dict[str, Any]:
    return {"namespace": args.namespace, "deployment": args.deployment, "service": args.service,
            "servicePort": args.service_port, "labelSelector": args.label_selector}


def service_selector(namespace: str, service: str) -> dict[str, str]:
    completed = kubectl(namespace, "get", "service", service, "-o", "json")
    return json.loads(completed.stdout).get("spec", {}).get("selector", {})


def replace_selector(namespace: str, service: str, selector: dict[str, str]) -> None:
    patch = json.dumps([{"op": "replace", "path": "/spec/selector", "value": selector}])
    kubectl(namespace, "patch", "service", service, "--type=json", "-p", patch)


def endpoint_available(namespace: str, service: str) -> bool:
    payload = json.loads(kubectl(namespace, "get", "endpoints", service, "-o", "json").stdout)
    return any(subset.get("addresses") for subset in payload.get("subsets", []))


def wait_for_endpoint_state(namespace: str, service: str, *, expected: bool, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if endpoint_available(namespace, service) is expected:
            return
        time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for Service endpoints expected={expected}.")


def unavailable_probes(seconds: int) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        probes.append({"at": now_utc(), "status_code": None, "latency_ms": 0.0, "success": False,
                       "error": "service has no ready endpoints"})
        time.sleep(1)
    return probes


def kubectl(namespace: str, *args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(kubectl_command("-n", namespace, *args), text=True, capture_output=True, env=kubectl_environment())
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "kubectl command failed")
    return completed


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
