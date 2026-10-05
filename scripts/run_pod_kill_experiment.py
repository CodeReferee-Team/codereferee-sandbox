#!/usr/bin/env python3
"""Run a Kubernetes Pod Kill experiment against a configured HTTP service target."""

from __future__ import annotations

import argparse
from http.client import RemoteDisconnected
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from collect_baseline import LABEL_SELECTOR, SERVICE, get_ready_pod, kubectl_command, kubectl_environment, percentile


NAMESPACE = "codereferee-sandbox"
SERVICE_PORT = 5678


def main() -> int:
    args = parse_args()
    target_metadata = resolve_target(args)
    started_at = time.monotonic()
    original_pod = get_ready_pod(args.namespace, args.label_selector, args.service)
    target_configuration = get_target_configuration(args.namespace, args.deployment)
    forward = start_port_forward(args.namespace, args.service, args.service_port, args.local_port)
    try:
        wait_for_service(forward, args.local_port, args.request_timeout_seconds)
        baseline = collect_probes(args.local_port, args.baseline_probes, args.request_timeout_seconds)
        fault_started_at = now_utc()
        delete_pod(args.namespace, original_pod["name"])
        observation = observe_recovery(
            args.namespace,
            original_pod["name"],
            args.label_selector,
            args.local_port,
            args.recovery_timeout_seconds,
            args.probe_interval_seconds,
            args.request_timeout_seconds,
            fault_started_at,
        )
        if observation["recovered"]:
            stop_process(forward)
            replacement_probe = verify_replacement_service(
                args.namespace, args.service, args.service_port, args.local_port, args.request_timeout_seconds
            )
            observation["probes"].append(replacement_probe)
            observation["last_http_status"] = replacement_probe["status_code"]
    finally:
        stop_process(forward)

    recovered = observation["replacement_pod_created"] and observation["recovered"]
    duration_millis = round((time.monotonic() - started_at) * 1000)
    all_probes = [*baseline["probes"], *observation["probes"]]
    metrics = metrics_from_probes(all_probes)
    metrics["restart_count"] = observation["replacement_restart_count"]
    metrics["recovery_seconds"] = observation["recovery_seconds"]
    result = {
        "exitCode": 0 if recovered else 1,
        "observationStatus": "observed",
        "stdout": f"{args.service} recovered after Pod Kill."
        if recovered
        else f"{args.service} did not recover before the deadline.",
        "stderr": "" if recovered else observation["failure_reason"],
        "timedOut": not recovered,
        "durationMillis": duration_millis,
        "serverStarted": recovered,
        "serverUrl": f"http://{args.service}.{args.namespace}.svc.cluster.local:{args.service_port}",
        "httpStatus": observation["last_http_status"],
        "browserLoaded": False,
        "pageTitle": None,
        "runCommand": ["kubectl", "delete", "pod", original_pod["name"], "-n", args.namespace],
        "schemaVersion": "chaos-v1",
        "baseline": {
            "pod": original_pod,
            "metrics": metrics_from_probes(baseline["probes"]),
        },
        "metrics": metrics,
        "probeTransport": "kubectl_port_forward",
        "target": {
            "name": target_metadata["name"],
            "deployment": args.deployment,
            "service": args.service,
            "service_port": args.service_port,
            "label_selector": args.label_selector,
        },
        "repository": target_metadata["repository"],
        "replicas": target_configuration["replicas"],
        "chaos_observation": {
            "type": "pod_kill",
            "target_kind": "Pod",
            "target_name": original_pod["name"],
            "target_pod_uid": original_pod["uid"],
            "namespace": args.namespace,
            "replicas": target_configuration["replicas"],
            "target_configuration": target_configuration,
            "kill_method": "kubectl_delete_pod",
            "delete_wait": False,
            "graceful_termination": True,
            "started_at": fault_started_at,
            "replacement_pod_created": observation["replacement_pod_created"],
            "replacement_pod_name": observation["replacement_pod_name"],
            "replacement_pod_uid": observation["replacement_pod_uid"],
            "replacement_restart_count": observation["replacement_restart_count"],
            "recovered": observation["recovered"],
            "recovered_at": observation["recovered_at"],
            "recovery_seconds": observation["recovery_seconds"],
            "kubernetes_events": observation["events"],
            "replacement_logs": observation["logs"],
            "observation_window": {
                "baseline_probe_count": args.baseline_probes,
                "recovery_timeout_seconds": args.recovery_timeout_seconds,
                "probe_interval_seconds": args.probe_interval_seconds,
                "request_timeout_seconds": args.request_timeout_seconds,
                "total_probe_count": len(all_probes),
                "failure_probe_count": len(all_probes) - successes_from_probes(all_probes),
            },
            "abort_condition": {
                "triggered": not recovered,
                "reason": "recovery_timeout" if not recovered else None,
            },
        },
        "source": {
            "real_execution_observed": True,
            "fixture": args.service == SERVICE,
            "target": args.service,
        },
    }
    write_result(result, args.output)
    return 0 if recovered else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Kubernetes Pod Kill experiment.")
    parser.add_argument(
        "--target-file",
        type=Path,
        help="Optional repository target JSON. CLI target flags take precedence over this file.",
    )
    parser.add_argument("--namespace")
    parser.add_argument("--deployment")
    parser.add_argument("--service")
    parser.add_argument("--service-port", type=int)
    parser.add_argument("--label-selector")
    parser.add_argument("--baseline-probes", type=int, default=5)
    parser.add_argument("--recovery-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--probe-interval-seconds", type=float, default=0.5)
    parser.add_argument("--request-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--local-port", type=int, default=18080)
    parser.add_argument("--output", type=Path, help="Optional JSON output path. Defaults to stdout.")
    args = parser.parse_args()
    if args.baseline_probes < 1:
        parser.error("--baseline-probes must be at least 1")
    return args


def resolve_target(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve a generic deployed repository target without hard-coding a repo.

    The target file describes an already deployed workload. It deliberately does
    not execute arbitrary repository configuration; the deployment stage remains
    a separate, policy-controlled concern.
    """
    target: dict[str, Any] = {}
    if args.target_file:
        try:
            target = json.loads(args.target_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Unable to read --target-file {args.target_file}: {exc}") from exc

    target_values = target.get("target", {})
    args.namespace = args.namespace or target_values.get("namespace") or NAMESPACE
    args.deployment = args.deployment or target_values.get("deployment") or SERVICE
    args.service = args.service or target_values.get("service") or SERVICE
    args.service_port = args.service_port or target_values.get("servicePort") or SERVICE_PORT
    args.label_selector = args.label_selector or target_values.get("labelSelector") or LABEL_SELECTOR

    if not all((args.namespace, args.deployment, args.service, args.service_port, args.label_selector)):
        raise SystemExit("A Chaos target requires namespace, deployment, service, servicePort, and labelSelector.")

    return {
        "name": target_values.get("name") or args.deployment,
        "repository": target.get("repository") or None,
    }


def start_port_forward(namespace: str, service: str, service_port: int, local_port: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        kubectl_command("-n", namespace, "port-forward", f"service/{service}", f"{local_port}:{service_port}"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=kubectl_environment(),
    )


def wait_for_service(process: subprocess.Popen[str], local_port: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"kubectl port-forward exited early: {stderr.strip()}")
        if probe_once(local_port, 0.2)["success"]:
            return
        time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for service port-forward on {local_port}")


def collect_probes(local_port: int, count: int, timeout_seconds: float) -> dict[str, Any]:
    return {"probes": [probe_once(local_port, timeout_seconds) for _ in range(count)]}


def observe_recovery(
    namespace: str,
    original_pod_name: str,
    label_selector: str,
    local_port: int,
    recovery_timeout_seconds: float,
    probe_interval_seconds: float,
    request_timeout_seconds: float,
    fault_started_at: str,
) -> dict[str, Any]:
    started_at = time.monotonic()
    deadline = started_at + recovery_timeout_seconds
    probes: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        probes.append(probe_once(local_port, request_timeout_seconds))
        replacement = find_replacement_pod(namespace, original_pod_name, label_selector)
        if replacement is not None:
            return {
                "replacement_pod_created": True,
                "replacement_pod_name": replacement["name"],
                "replacement_pod_uid": replacement["uid"],
                "replacement_restart_count": replacement["restart_count"],
                "recovered": True,
                "recovered_at": now_utc(),
                "recovery_seconds": round(time.monotonic() - started_at, 2),
                "probes": probes,
                "last_http_status": probes[-1]["status_code"] if probes else None,
                "events": relevant_events(namespace, fault_started_at),
                "logs": kubectl(namespace, "logs", replacement["name"], "--tail", "50").stdout.strip(),
                "failure_reason": "",
            }
        time.sleep(probe_interval_seconds)

    return {
        "replacement_pod_created": False,
        "replacement_pod_name": None,
        "replacement_pod_uid": None,
        "replacement_restart_count": None,
        "recovered": False,
        "recovered_at": None,
        "recovery_seconds": round(time.monotonic() - started_at, 2),
        "probes": probes,
        "last_http_status": probes[-1]["status_code"] if probes else None,
        "events": relevant_events(namespace, fault_started_at),
        "logs": "",
        "failure_reason": f"Target service did not recover within {recovery_timeout_seconds} seconds.",
    }


def find_replacement_pod(
    namespace: str, original_pod_name: str, label_selector: str
) -> dict[str, Any] | None:
    response = kubectl(namespace, "get", "pods", "-l", label_selector, "-o", "json")
    for pod in json.loads(response.stdout).get("items", []):
        if pod.get("metadata", {}).get("name") == original_pod_name:
            continue
        conditions = pod.get("status", {}).get("conditions", [])
        ready = any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions)
        if ready:
            statuses = pod.get("status", {}).get("containerStatuses", [])
            return {
                "name": pod["metadata"]["name"],
                "uid": pod["metadata"].get("uid"),
                "restart_count": sum(status.get("restartCount", 0) for status in statuses),
            }
    return None


def relevant_events(namespace: str, fault_started_at: str) -> list[dict[str, str | None]]:
    response = kubectl(namespace, "get", "events", "--sort-by=.lastTimestamp", "-o", "json")
    threshold = parse_utc(fault_started_at) - timedelta(seconds=2)
    events: list[dict[str, str | None]] = []
    for event in json.loads(response.stdout).get("items", []):
        timestamp = event.get("lastTimestamp") or event.get("eventTime") or event.get("metadata", {}).get("creationTimestamp")
        if not timestamp or parse_utc(timestamp) < threshold:
            continue
        involved = event.get("involvedObject", {})
        events.append(
            {
                "at": timestamp,
                "type": event.get("type"),
                "reason": event.get("reason"),
                "message": event.get("message"),
                "object_kind": involved.get("kind"),
                "object_name": involved.get("name"),
            }
        )
    return events


def delete_pod(namespace: str, pod_name: str) -> None:
    kubectl(namespace, "delete", "pod", pod_name, "--wait=false")


def probe_once(local_port: int, timeout_seconds: float) -> dict[str, Any]:
    started_at = time.monotonic()
    status_code: int | None = None
    error = ""
    try:
        with urlopen(f"http://127.0.0.1:{local_port}/", timeout=timeout_seconds) as response:
            status_code = response.status
            response.read()
    except HTTPError as exc:
        status_code = exc.code
        error = str(exc)
    except (URLError, RemoteDisconnected, TimeoutError, OSError) as exc:
        error = str(getattr(exc, "reason", exc))
    latency_ms = round((time.monotonic() - started_at) * 1000, 2)
    return {
        "at": now_utc(),
        "status_code": status_code,
        "latency_ms": latency_ms,
        "success": status_code is not None and 200 <= status_code < 400,
        "error": error,
    }


def verify_replacement_service(
    namespace: str, service: str, service_port: int, local_port: int, timeout_seconds: float
) -> dict[str, Any]:
    process = start_port_forward(namespace, service, service_port, local_port)
    try:
        wait_for_service(process, local_port, timeout_seconds)
        return probe_once(local_port, timeout_seconds)
    finally:
        stop_process(process)


def metrics_from_probes(probes: list[dict[str, Any]]) -> dict[str, Any]:
    successes = successes_from_probes(probes)
    availability = successes / len(probes) if probes else 0.0
    return {
        "availability": availability,
        "p95_latency_ms": percentile([probe["latency_ms"] for probe in probes], 95),
        "error_rate": 1 - availability,
        "probe_count": len(probes),
        "success_count": successes,
        "failure_count": len(probes) - successes,
        "error_rate_denominator": "all HTTP GET / probes collected during baseline, recovery, and replacement verification; readiness wait probes are excluded",
        "cpu_usage_percent": None,
        "memory_usage_mb": None,
        "restart_count": 0,
    }


def successes_from_probes(probes: list[dict[str, Any]]) -> int:
    return sum(probe["success"] for probe in probes)


def get_target_configuration(namespace: str, deployment_name: str) -> dict[str, Any]:
    deployment = json.loads(kubectl(namespace, "get", "deployment", deployment_name, "-o", "json").stdout)
    template_spec = deployment.get("spec", {}).get("template", {}).get("spec", {})
    containers = template_spec.get("containers", [])
    container = containers[0] if containers else {}
    return {
        "replicas": deployment.get("spec", {}).get("replicas", 1),
        "readiness_probe": normalize_probe(container.get("readinessProbe")),
        "liveness_probe": normalize_probe(container.get("livenessProbe")),
        "startup_probe": normalize_probe(container.get("startupProbe")),
        "termination_grace_period_seconds": template_spec.get("terminationGracePeriodSeconds", 30),
    }


def normalize_probe(probe: object) -> dict[str, Any] | None:
    if not isinstance(probe, dict):
        return None
    return {
        "type": "tcp_socket" if "tcpSocket" in probe else "http_get" if "httpGet" in probe else "exec" if "exec" in probe else "unknown",
        "initial_delay_seconds": probe.get("initialDelaySeconds"),
        "period_seconds": probe.get("periodSeconds"),
        "timeout_seconds": probe.get("timeoutSeconds"),
        "failure_threshold": probe.get("failureThreshold"),
        "success_threshold": probe.get("successThreshold"),
    }


def kubectl(namespace: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        kubectl_command("-n", namespace, *args),
        check=True,
        capture_output=True,
        text=True,
        env=kubectl_environment(),
    )


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def write_result(result: dict[str, Any], output: Path | None) -> None:
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if output is None:
        print(payload)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload + "\n", encoding="utf-8")
    print(f"Chaos experiment result written to {output}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, TimeoutError) as exc:
        print(f"Pod Kill experiment failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
