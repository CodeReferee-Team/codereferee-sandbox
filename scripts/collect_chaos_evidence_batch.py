#!/usr/bin/env python3
"""Collect repeatable, real Chaos v1 observations as a JSONL evidence batch."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_SCRIPT = PROJECT_ROOT / "scripts" / "run_pod_kill_experiment.py"


def main() -> int:
    args = parse_args()
    output = args.output or default_output_path()
    output.parent.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    for number in range(1, args.runs + 1):
        completed = subprocess.run(
            [
                sys.executable,
                str(EXPERIMENT_SCRIPT),
                "--baseline-probes",
                str(args.baseline_probes),
                "--recovery-timeout-seconds",
                str(args.recovery_timeout_seconds),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        observation = parse_observation(completed)
        records.append(
            {
                "schemaVersion": "chaos-evidence-v1",
                "collectedAt": now_utc(),
                "run": number,
                "scenario": "fixture_pod_kill",
                "source": "real_kubernetes_execution",
                "observation": observation,
            }
        )
        print(run_summary(number, args.runs, observation))

    output.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    observed_count = sum(record["observation"].get("observationStatus") == "observed" for record in records)
    print(f"Wrote {len(records)} records ({observed_count} observed) to {output}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect a batch of actual Chaos v1 observations.")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--baseline-probes", type=int, default=20)
    parser.add_argument("--recovery-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.baseline_probes < 1:
        parser.error("--baseline-probes must be at least 1")
    return args


def parse_observation(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        observation = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "observationStatus": "infrastructure_error",
            "exitCode": None,
            "timedOut": False,
            "stderr": completed.stderr.strip() or "Chaos experiment returned invalid JSON.",
            "rawStdout": completed.stdout.strip(),
        }
    if completed.returncode != 0 and not observation.get("stderr"):
        observation["stderr"] = completed.stderr.strip() or "Chaos experiment failed."
    return observation


def run_summary(number: int, total: int, observation: dict[str, Any]) -> str:
    metrics = observation.get("metrics") or {}
    return (
        f"[{number}/{total}] status={observation.get('observationStatus')} "
        f"exitCode={observation.get('exitCode')} "
        f"recoverySeconds={metrics.get('recovery_seconds')} "
        f"errorRate={metrics.get('error_rate')}"
    )


def default_output_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return PROJECT_ROOT / "data" / "actual" / f"chaos-v1-pod-kill-{timestamp}.jsonl"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
