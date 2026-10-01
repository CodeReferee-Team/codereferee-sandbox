#!/usr/bin/env python3
"""Collect a reproducible healthy baseline from the Chaos v1 fixture service."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


NAMESPACE = "codereferee-sandbox"
SERVICE = "fixture-api"
LABEL_SELECTOR = "app.kubernetes.io/name=fixture-api"


def kubectl_environment() -> dict[str, str]:
    """Run local Docker Desktop kubectl without a global HTTP proxy.

    Some developer environments set HTTP(S)_PROXY to a local debugging proxy.
    Kubernetes API traffic for Docker Desktop is local control-plane traffic and
    must not be routed through that proxy.
    """
    environment = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment.pop(key, None)
    return environment


def main() -> int:
    args = parse_args()
    started_at = time.monotonic()
    pod = get_ready_pod(args.namespace)
    probe_result = probe_service(args.namespace, args.probes, args.timeout_seconds, args.local_port)
    logs = kubectl(args.namespace, "logs", pod["name"], "--tail", str(args.log_lines)).stdout.strip()

    result = {
        "schemaVersion": "chaos-v1",
        "capturedAt": now_utc(),
        "fixture": SERVICE,
        "baseline": {
            "healthy": pod["ready"],
            "pod": pod,
            "metrics": probe_result["metrics"],
            "http": probe_result["http"],
            "logs": logs,
        },
        "durationMillis": duration_ms(started_at),
    }
    write_result(result, args.output)
    return 0 if pod["ready"] and probe_result["metrics"]["availability"] == 1.0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect a healthy Chaos v1 fixture baseline.")
    parser.add_argument("--namespace", default=NAMESPACE)
    parser.add_argument("--probes", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--local-port", type=int, default=18080)
    parser.add_argument("--log-lines", type=int, default=50)
    parser.add_argument("--output", type=Path, help="Optional JSON output path. Defaults to stdout.")
    args = parser.parse_args()
    if args.probes < 1:
        parser.error("--probes must be at least 1")
    return args


def get_ready_pod(namespace: str) -> dict[str, Any]:
    response = kubectl(namespace, "get", "pods", "-l", LABEL_SELECTOR, "-o", "json")
    pods = json.loads(response.stdout).get("items", [])
    ready_pods = [pod for pod in pods if is_ready(pod)]
    if not ready_pods:
        raise RuntimeError(f"No Ready {SERVICE} Pod found in namespace {namespace}")

    pod = ready_pods[0]
    statuses = pod.get("status", {}).get("containerStatuses", [])
    return {
        "name": pod["metadata"]["name"],
        "uid": pod["metadata"].get("uid"),
        "phase": pod.get("status", {}).get("phase"),
        "ready": True,
        "restart_count": sum(status.get("restartCount", 0) for status in statuses),
        "node": pod.get("spec", {}).get("nodeName"),
    }


def probe_service(namespace: str, probes: int, timeout_seconds: float, local_port: int) -> dict[str, Any]:
    process = subprocess.Popen(
        [
            "kubectl",
            "-n",
            namespace,
            "port-forward",
            f"service/{SERVICE}",
            f"{local_port}:5678",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=kubectl_environment(),
    )
    try:
        wait_for_port_forward(process, local_port, timeout_seconds)
        status_codes: list[int | None] = []
        latencies_ms: list[float] = []
        errors: list[str] = []
        for _ in range(probes):
            latency_started = time.monotonic()
            try:
                with urlopen(f"http://127.0.0.1:{local_port}/", timeout=timeout_seconds) as response:
                    status_codes.append(response.status)
                    response.read()
            except URLError as exc:
                status_codes.append(None)
                errors.append(str(exc.reason))
            finally:
                latencies_ms.append((time.monotonic() - latency_started) * 1000)

        success_count = sum(code is not None and 200 <= code < 400 for code in status_codes)
        availability = success_count / probes
        return {
            "metrics": {
                "availability": availability,
                "p95_latency_ms": percentile(latencies_ms, 95),
                "error_rate": 1 - availability,
                "cpu_usage_percent": None,
                "memory_usage_mb": None,
                "restart_count": 0,
            },
            "http": {
                "probe_count": probes,
                "success_count": success_count,
                "status_codes": status_codes,
                "latencies_ms": [round(value, 2) for value in latencies_ms],
                "errors": errors,
            },
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def wait_for_port_forward(process: subprocess.Popen[str], local_port: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"kubectl port-forward exited early: {stderr.strip()}")
        try:
            with urlopen(f"http://127.0.0.1:{local_port}/", timeout=0.2) as response:
                response.read()
                return
        except URLError:
            time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for fixture service port-forward on {local_port}")


def is_ready(pod: dict[str, Any]) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    return any(condition.get("type") == "Ready" and condition.get("status") == "True" for condition in conditions)


def kubectl(namespace: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "-n", namespace, *args],
        check=True,
        capture_output=True,
        text=True,
        env=kubectl_environment(),
    )


def percentile(values: list[float], percentile_value: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 2)
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile_value / 100)
    return round(ordered[index], 2)


def duration_ms(started_at: float) -> int:
    return round((time.monotonic() - started_at) * 1000)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_result(result: dict[str, Any], output: Path | None) -> None:
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if output is None:
        print(payload)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload + "\n", encoding="utf-8")
    print(f"Baseline written to {output}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, TimeoutError) as exc:
        print(f"Baseline collection failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
