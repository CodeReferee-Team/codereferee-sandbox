#!/usr/bin/env python3
"""Install the metric collection wiring into the Sandbox cluster.

Prometheus itself must not live here.  Chaos can kill this cluster at any
moment and the cluster is torn down between requests, so anything that stores
samples locally loses exactly the window we need.  What runs here is a
Prometheus *agent*: it scrapes, and immediately forwards over remote_write to
the long-lived Prometheus outside.  Nothing is stored and nothing is queryable
on this side.

``requestId`` is stamped on every sample as an external label.  Without it the
receiver cannot tell one validation run's samples from another's.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

from collect_baseline import kubectl_command, kubectl_environment


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "observability.yaml"
NAMESPACE = "codereferee-observability"
WORKLOADS = ("daemonset/cadvisor", "daemonset/node-exporter", "deployment/codereferee-agent")


def main() -> int:
    args = parse_args()
    if args.uninstall:
        uninstall()
        print(f"removed namespace {NAMESPACE}")
        return 0

    rendered = render(args.remote_write_url, args.request_id, args.cluster_name)
    apply(rendered)
    for workload in WORKLOADS:
        wait_ready(workload, args.timeout)
    wait_agent_config(config_hash(rendered), args.timeout)
    print(f"observability wiring is ready in {NAMESPACE}")
    print(f"  requestId    = {args.request_id}")
    print(f"  remote_write = {args.remote_write_url}")
    return 0


def render(remote_write_url: str, request_id: str, cluster_name: str) -> str:
    """Substitute Sandbox-owned placeholders, as k8s/quickbyte-demo.yaml does."""
    values = {
        "${REMOTE_WRITE_URL}": remote_write_url,
        "${REQUEST_ID}": request_id,
        "${CLUSTER_NAME}": cluster_name,
    }
    rendered = MANIFEST.read_text(encoding="utf-8")
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    # Substituted last so the hash covers the already-rendered agent config.
    rendered = rendered.replace("${CONFIG_HASH}", config_hash(rendered))
    remaining = sorted(set(re.findall(r"\$\{[A-Z_]+\}", rendered)))
    if remaining:
        raise RuntimeError(f"Manifest still has unsubstituted placeholders: {', '.join(remaining)}")
    return rendered


def config_hash(rendered: str) -> str:
    """Hash the agent config so a changed config rolls the agent pod."""
    start = rendered.index("  prometheus.yml: |")
    end = rendered.index("\n---", start)
    return hashlib.sha256(rendered[start:end].encode("utf-8")).hexdigest()[:16]


def apply(manifest: str) -> None:
    run(kubectl_command("apply", "-f", "-"), input_text=manifest)


def wait_ready(workload: str, timeout: int) -> None:
    # Not `kubectl wait` on a pod selector: between apply and the controller
    # creating pods there is a window where no pod matches, and `wait` fails
    # immediately with "no matching resources found" instead of waiting.
    # `rollout status` waits for the controller as well as for readiness.
    run(kubectl_command("rollout", "status", workload, "--namespace", NAMESPACE,
                        f"--timeout={timeout}s"))


def wait_agent_config(expected: str, timeout: int) -> None:
    """Wait until a Ready agent pod is actually running the config we just applied.

    `rollout status` has a narrow window right after apply where the old
    ReplicaSet still looks fully available and it reports success before the new
    ReplicaSet exists.  A stale agent keeps writing to the previous remote_write
    URL and stamping the previous requestId, which is silent and wrong rather
    than loud and broken, so confirm the running pod by its config hash.
    """
    deadline = time.monotonic() + timeout
    while True:
        completed = run(kubectl_command(
            "get", "pod", "--namespace", NAMESPACE,
            "--selector", "codereferee.io/component=agent", "--output", "json",
        ))
        for pod in json.loads(completed.stdout).get("items", []):
            annotations = pod["metadata"].get("annotations", {})
            if annotations.get("codereferee.io/config-hash") != expected:
                continue
            if pod["metadata"].get("deletionTimestamp"):
                continue
            conditions = pod.get("status", {}).get("conditions", [])
            if any(c["type"] == "Ready" and c["status"] == "True" for c in conditions):
                return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"No Ready agent pod is running config {expected} after {timeout}s.")
        time.sleep(2)


def uninstall() -> None:
    subprocess.run(
        kubectl_command("delete", "namespace", NAMESPACE, "--ignore-not-found=true", "--wait=false"),
        text=True, capture_output=True, env=kubectl_environment(),
    )


def run(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, input=input_text, text=True, capture_output=True,
                               env=kubectl_environment())
    if completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise RuntimeError(f"{' '.join(command[:3])}: {message}")
    return completed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-write-url",
                        default="http://host.docker.internal:9090/api/v1/write",
                        help="Receiving Prometheus. It needs --web.enable-remote-write-receiver.")
    parser.add_argument("--request-id", default="local",
                        help="Stamped on every sample so the receiver can separate runs.")
    parser.add_argument("--cluster-name", default="codereferee")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--uninstall", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
