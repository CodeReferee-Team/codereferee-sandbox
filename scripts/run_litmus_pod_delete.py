#!/usr/bin/env python3
"""Run the Litmus Pod Delete fault for a deployed repository target.

The target is supplied by the repository executor; this script contains no
repository-specific namespace, image, or label values.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from collect_baseline import kubectl_environment
from run_pod_kill_experiment import collect_probes, metrics_from_probes, probe_once, start_port_forward, stop_process, wait_for_service


ROLE = "litmus-agent-chaos-operator-litmus-admin"


def main() -> int:
    args = parse_args()
    target = load_target(args)
    required = ("namespace", "deployment", "labelSelector")
    missing = [name for name in required if not target.get(name)]
    if missing:
        raise SystemExit(f"target file missing: {', '.join(missing)}")
    name = "codereferee-pod-delete-" + safe_name(target.get("name") or target["deployment"]) + f"-{int(time.time())}"
    forward = None
    baseline: dict[str, Any] | None = None
    try:
        if target.get("service") and target.get("servicePort"):
            forward = start_port_forward(target["namespace"], target["service"], int(target["servicePort"]), args.local_port)
            wait_for_service(forward, args.local_port, args.request_timeout_seconds)
            baseline = collect_probes(args.local_port, args.baseline_probes, args.request_timeout_seconds)
        apply(target["namespace"], name, target["deployment"], target["labelSelector"])
        result, recovery_probes = wait_for_result(target["namespace"], name, args.timeout_seconds, args.local_port if forward else None,
                                                   args.request_timeout_seconds)
    finally:
        if forward:
            stop_process(forward)
    output = {"schemaVersion": "litmus-v1", "scenario": "pod_delete", "target": target,
              "chaosEngine": name, "chaosResult": result}
    if baseline is not None:
        probes = [*baseline["probes"], *recovery_probes]
        output["baseline"] = {"metrics": metrics_from_probes(baseline["probes"])}
        output["metrics"] = metrics_from_probes(probes)
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result.get("verdict") == "Pass" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run generic Litmus Pod Delete.")
    parser.add_argument("--target-file", type=Path)
    parser.add_argument("--namespace")
    parser.add_argument("--deployment")
    parser.add_argument("--label-selector")
    parser.add_argument("--service")
    parser.add_argument("--service-port", type=int)
    parser.add_argument("--baseline-probes", type=int, default=20)
    parser.add_argument("--request-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--local-port", type=int, default=18080)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--output", type=Path, help="Optional JSON evidence output path.")
    return parser.parse_args()


def load_target(args: argparse.Namespace) -> dict[str, Any]:
    if args.target_file:
        return json.loads(args.target_file.read_text(encoding="utf-8")).get("target", {})
    return {"namespace": args.namespace, "deployment": args.deployment, "labelSelector": args.label_selector,
            "service": args.service, "servicePort": args.service_port}


def apply(namespace: str, engine: str, deployment: str, selector: str) -> None:
    copy_experiment(namespace)
    manifest = f'''apiVersion: v1
kind: ServiceAccount
metadata:
  name: litmus-admin
  namespace: {namespace}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: codereferee-litmus-admin
  namespace: {namespace}
subjects:
  - kind: ServiceAccount
    name: litmus-admin
    namespace: {namespace}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {ROLE}
---
apiVersion: litmuschaos.io/v1alpha1
kind: ChaosEngine
metadata:
  name: {engine}
  namespace: {namespace}
spec:
  appinfo:
    appns: {namespace}
    applabel: "{selector}"
    appkind: deployment
  jobCleanUpPolicy: delete
  engineState: active
  chaosServiceAccount: litmus-admin
  experiments:
    - name: pod-delete
      spec:
        components:
          env:
            - name: TOTAL_CHAOS_DURATION
              value: "15"
            - name: CHAOS_INTERVAL
              value: "5"
            - name: FORCE
              value: "true"
'''
    completed = subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, text=True, capture_output=True,
                               env=kubectl_environment())
    if completed.returncode:
        raise SystemExit(completed.stderr.strip() or completed.stdout.strip())


def copy_experiment(namespace: str) -> None:
    """Copy the official chart's namespaced Pod Delete definition to the target."""
    source = subprocess.run(["kubectl", "get", "chaosexperiment", "pod-delete", "-n", "litmus", "-o", "json"],
                            text=True, capture_output=True, env=kubectl_environment(), check=True)
    experiment = json.loads(source.stdout)
    experiment.pop("status", None)
    metadata = experiment["metadata"]
    for field in ("creationTimestamp", "generation", "resourceVersion", "uid", "managedFields", "annotations"):
        metadata.pop(field, None)
    metadata["namespace"] = namespace
    copied = subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(experiment), text=True,
                            capture_output=True, env=kubectl_environment())
    if copied.returncode:
        raise SystemExit(copied.stderr.strip() or copied.stdout.strip())


def wait_for_result(namespace: str, engine: str, timeout: int, local_port: int | None,
                    request_timeout_seconds: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout
    probes: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        if local_port:
            probes.append(probe_once(local_port, request_timeout_seconds))
        result = subprocess.run(["kubectl", "get", "chaosresult", "-n", namespace, "-o", "json"], text=True,
                                capture_output=True, env=kubectl_environment())
        if result.returncode == 0:
            for item in json.loads(result.stdout).get("items", []):
                if item.get("metadata", {}).get("name", "").startswith(engine + "-"):
                    status = item.get("status", {})
                    verdict = status.get("experimentStatus", {}).get("verdict")
                    if verdict in {"Pass", "Fail", "Stopped"}:
                        return (
                            {"name": item["metadata"]["name"], "verdict": verdict,
                             "phase": status.get("experimentStatus", {}).get("phase"), "raw": status},
                            probes,
                        )
        time.sleep(2)
    raise SystemExit(f"Timed out waiting for Litmus result from {engine}")


def safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:40]


if __name__ == "__main__":
    raise SystemExit(main())
