#!/usr/bin/env python3
"""Install the metric collection wiring into the Sandbox cluster.

Prometheus itself must not live here.  Chaos can kill this cluster at any
moment and the cluster is torn down between requests, so anything that stores
samples locally loses exactly the window we need.  What runs here is a
Prometheus *agent*: it scrapes, and immediately forwards over remote_write to
the long-lived Prometheus outside. A bounded local WAL buffers pending samples;
forced shutdown can still lose samples not acknowledged by the receiver.

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
from urllib.parse import urlparse

from collect_baseline import kubectl_command, kubectl_environment


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "observability.yaml"
NAMESPACE = "codereferee-observability"
WORKLOADS = ("daemonset/cadvisor", "daemonset/node-exporter", "deployment/codereferee-agent")


def main() -> int:
    args = parse_args()
    if args.uninstall:
        uninstall(observation_namespace(args.request_id, args.target_namespace or ('codereferee-' + args.request_id)))
        return 0

    rendered = render(args.remote_write_url, args.request_id, args.cluster_name, args.target_namespace)
    namespace = observation_namespace(args.request_id, args.target_namespace or ('codereferee-' + args.request_id))
    apply(rendered)
    for workload in WORKLOADS:
        wait_ready(workload, args.timeout, namespace)
    wait_agent_config(config_hash(rendered), args.timeout, namespace)
    print(f"observability wiring is ready in {namespace}")
    print(f"  requestId    = {args.request_id}")
    print(f"  remote_write = {args.remote_write_url}")
    return 0


def observation_namespace(request_id: str, target_namespace: str) -> str:
    return 'codereferee-metrics-' + hashlib.sha256((request_id + ':' + target_namespace).encode()).hexdigest()[:16]


def render(remote_write_url: str, request_id: str, cluster_name: str, target_namespace: str | None = None) -> str:
    """Substitute Sandbox-owned placeholders, as k8s/quickbyte-demo.yaml does."""
    target_namespace = target_namespace or ('codereferee-' + request_id)
    remote = urlparse(remote_write_url)
    if remote.scheme not in {'http', 'https'} or not remote.hostname or remote.username or remote.password or remote.query or remote.fragment or any(c in remote_write_url for c in '\n\r"\\'):
        raise ValueError('remote-write URL must be an HTTP receiver URL without embedded credentials.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', request_id) or not re.fullmatch(r'[a-z0-9-]{1,63}', cluster_name):
        raise ValueError('Invalid request or cluster identity.')
    if not re.fullmatch(r'codereferee-[a-z0-9-]{1,51}', target_namespace):
        raise ValueError('Observation is restricted to a CodeReferee request namespace.')
    values = {
        "${REMOTE_WRITE_URL}": remote_write_url,
        "${REQUEST_ID}": request_id,
        "${CLUSTER_NAME}": cluster_name,
        '${TARGET_NAMESPACE}': target_namespace,
    }
    rendered = MANIFEST.read_text(encoding="utf-8")
    # PriorityClass remains a shared cluster prerequisite; namespaced resources
    # and discovery are private to this request, not a mutable global Agent.
    rendered = rendered.replace('namespace: codereferee-observability', 'namespace: ' + observation_namespace(request_id, target_namespace))
    rendered = rendered.replace('name: codereferee-observability\n  labels:', 'name: ' + observation_namespace(request_id, target_namespace) + '\n  labels:')
    rendered = rendered.replace('names: [codereferee-observability]', 'names: [' + observation_namespace(request_id, target_namespace) + ']')
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


def wait_ready(workload: str, timeout: int, namespace: str = NAMESPACE) -> None:
    # Not `kubectl wait` on a pod selector: between apply and the controller
    # creating pods there is a window where no pod matches, and `wait` fails
    # immediately with "no matching resources found" instead of waiting.
    # `rollout status` waits for the controller as well as for readiness.
    run(kubectl_command("rollout", "status", workload, "--namespace", namespace,
                        f"--timeout={timeout}s"))


def wait_agent_config(expected: str, timeout: int, namespace: str = NAMESPACE) -> None:
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
            "get", "pod", "--namespace", namespace,
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


def uninstall(namespace: str) -> None:
    if not re.fullmatch(r'codereferee-metrics-[a-f0-9]{16}', namespace):
        raise ValueError('Refusing cleanup outside an exact observation namespace.')
    run(kubectl_command("delete", "namespace", namespace, "--ignore-not-found=true", "--wait=true", '--timeout=60s'))


def run(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, input=input_text, text=True, capture_output=True,
                               env=kubectl_environment(), timeout=180)
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
    parser.add_argument('--target-namespace')
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--uninstall", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
