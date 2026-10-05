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
from run_pod_kill_experiment import (
    collect_probes,
    get_ready_pod,
    get_target_configuration,
    metrics_from_probes,
    probe_once,
    start_port_forward,
    stop_process,
    wait_for_service,
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
    forward = start_port_forward(target["namespace"], target["service"], int(target["servicePort"]), args.local_port)
    try:
        wait_for_service(forward, args.local_port, args.request_timeout_seconds)
        baseline = collect_probes(args.local_port, args.baseline_probes, args.request_timeout_seconds)
        fault_started = time.monotonic()
        scale(target["namespace"], target["deployment"], 0)
        fault_probes = collect_for_seconds(args.local_port, args.fault_seconds, args.request_timeout_seconds)
        scale(target["namespace"], target["deployment"], original_replicas)
        stop_process(forward)
        replacement_pod = wait_for_ready_pod(
            target["namespace"], target["labelSelector"], target["deployment"], args.recovery_timeout_seconds
        )
        forward = start_port_forward(target["namespace"], target["service"], int(target["servicePort"]), args.local_port)
        wait_for_service(forward, args.local_port, args.recovery_timeout_seconds)
        recovery_probes = collect_probes(args.local_port, args.recovery_probes, args.request_timeout_seconds)
    finally:
        scale(target["namespace"], target["deployment"], original_replicas)
        stop_process(forward)

    probes = [*baseline["probes"], *fault_probes, *recovery_probes["probes"]]
    denominator = "all HTTP GET / probes collected during baseline, scale-down, and recovery; readiness wait probes are excluded"
    baseline_metrics = metrics_from_probes(baseline["probes"])
    baseline_metrics["error_rate_denominator"] = "all HTTP GET / probes collected during baseline; readiness wait probes are excluded"
    observed_metrics = metrics_from_probes(probes)
    observed_metrics["error_rate_denominator"] = denominator
    output = {
        "schemaVersion": "chaos-v1",
        "scenario": "deployment_scale_down",
        "observationStatus": "observed",
        "target": target,
        "replicas": original_replicas,
        "baseline": {"metrics": baseline_metrics},
        "metrics": observed_metrics,
        "chaos_observation": {
            "type": "deployment_scale_down",
            "kill_method": "kubectl_scale",
            "started_at": started_at,
            "recovered_at": now_utc(),
            "recovery_seconds": round(time.monotonic() - fault_started, 2),
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
    }
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


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


def collect_for_seconds(local_port: int, seconds: int, timeout_seconds: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + seconds
    probes: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        probes.append(probe_once(local_port, timeout_seconds))
    return probes


def wait_for_ready_pod(namespace: str, label_selector: str, deployment: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            return get_ready_pod(namespace, label_selector, deployment)
        except RuntimeError as exc:
            last_error = str(exc)
            time.sleep(1)
    raise TimeoutError(last_error or f"Timed out waiting for Ready Pod in {namespace}")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
