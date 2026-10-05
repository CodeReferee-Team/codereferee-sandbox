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
    initial_pod_uids = {pod["uid"] for pod in ready_pods(target)}
    configuration = get_target_configuration(target["namespace"], target["deployment"])
    started_at = now_utc()
    forward = start_port_forward(target["namespace"], target["service"], target["servicePort"], args.local_port)
    try:
        wait_for_service(forward, args.local_port, args.request_timeout_seconds)
        baseline = collect_probes(args.local_port, args.baseline_probes, args.request_timeout_seconds)
        fault_started = time.monotonic()
        kubectl(target["namespace"], "rollout", "restart", f"deployment/{target['deployment']}")
        fault_probes = collect_until_replacement(
            target, initial_pod_uids, args.local_port, args.request_timeout_seconds, args.recovery_timeout_seconds
        )
        replacement = wait_for_replacement(target, initial_pod_uids, args.recovery_timeout_seconds)
        kubectl(target["namespace"], "rollout", "status", f"deployment/{target['deployment']}",
                f"--timeout={args.recovery_timeout_seconds}s")
        stop_process(forward)
        forward = start_port_forward(target["namespace"], target["service"], target["servicePort"], args.local_port)
        wait_for_service(forward, args.local_port, args.recovery_timeout_seconds)
        recovery_probes = collect_probes(args.local_port, args.recovery_probes, args.request_timeout_seconds)
    finally:
        stop_process(forward)

    probes = [*baseline["probes"], *fault_probes, *recovery_probes["probes"]]
    denominator = "all HTTP GET / probes collected during baseline, rollout restart, and replacement verification; readiness wait probes are excluded"
    baseline_metrics = metrics_from_probes(baseline["probes"])
    baseline_metrics["error_rate_denominator"] = "all HTTP GET / probes collected during baseline; readiness wait probes are excluded"
    observed_metrics = metrics_from_probes(probes)
    observed_metrics["error_rate_denominator"] = denominator
    output = {
        "schemaVersion": "chaos-v1", "scenario": "rollout_restart", "observationStatus": "observed",
        "target": target, "replicas": configuration["replicas"], "baseline": {"metrics": baseline_metrics},
        "metrics": observed_metrics,
        "chaos_observation": {
            "type": "rollout_restart", "kill_method": "kubectl_rollout_restart", "started_at": started_at,
            "recovered_at": now_utc(), "recovery_seconds": round(time.monotonic() - fault_started, 2),
            "target_pod_uid": source_pod["uid"], "replacement_pod_uid": replacement["uid"],
            "target_configuration": configuration,
            "rollout_operation": {"deployment": target["deployment"], "strategy": "kubectl rollout restart"},
            "observation_window": {"baseline_probe_count": args.baseline_probes,
                                   "recovery_timeout_seconds": args.recovery_timeout_seconds,
                                   "error_rate_denominator": denominator},
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


def wait_for_replacement(target: dict[str, Any], initial_pod_uids: set[str], timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        replacements = [pod for pod in ready_pods(target) if pod["uid"] not in initial_pod_uids]
        if replacements:
            return replacements[0]
        time.sleep(1)
    raise TimeoutError("Timed out waiting for a replacement Ready Pod after rollout restart.")


def collect_until_replacement(target: dict[str, Any], initial_pod_uids: set[str], local_port: int,
                              timeout_seconds: float, recovery_timeout_seconds: int) -> list[dict[str, Any]]:
    from run_pod_kill_experiment import probe_once
    deadline = time.monotonic() + recovery_timeout_seconds
    probes: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        probes.append(probe_once(local_port, timeout_seconds))
        if any(pod["uid"] not in initial_pod_uids for pod in ready_pods(target)):
            return probes
    raise TimeoutError("Timed out observing rollout replacement.")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
