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


ROLE = "litmus-agent-chaos-operator-litmus-admin"


def main() -> int:
    args = parse_args()
    target = json.loads(args.target_file.read_text(encoding="utf-8")).get("target", {})
    required = ("namespace", "deployment", "labelSelector")
    missing = [name for name in required if not target.get(name)]
    if missing:
        raise SystemExit(f"target file missing: {', '.join(missing)}")
    name = "codereferee-pod-delete-" + safe_name(target.get("name") or target["deployment"])
    apply(target["namespace"], name, target["deployment"], target["labelSelector"])
    result = wait_for_result(target["namespace"], name, args.timeout_seconds)
    output = {"schemaVersion": "litmus-v1", "scenario": "pod_delete", "target": target,
              "chaosEngine": name, "chaosResult": result}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if result.get("verdict") == "Pass" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run generic Litmus Pod Delete.")
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    return parser.parse_args()


def apply(namespace: str, engine: str, deployment: str, selector: str) -> None:
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
    completed = subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, text=True, capture_output=True)
    if completed.returncode:
        raise SystemExit(completed.stderr.strip() or completed.stdout.strip())


def wait_for_result(namespace: str, engine: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(["kubectl", "get", "chaosresult", "-n", namespace, "-o", "json"], text=True, capture_output=True)
        if result.returncode == 0:
            for item in json.loads(result.stdout).get("items", []):
                if item.get("metadata", {}).get("name", "").startswith(engine + "-"):
                    status = item.get("status", {})
                    verdict = status.get("experimentStatus", {}).get("verdict")
                    if verdict in {"Pass", "Fail", "Stopped"}:
                        return {"name": item["metadata"]["name"], "verdict": verdict,
                                "phase": status.get("experimentStatus", {}).get("phase"), "raw": status}
        time.sleep(2)
    raise SystemExit(f"Timed out waiting for Litmus result from {engine}")


def safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:40]


if __name__ == "__main__":
    raise SystemExit(main())
