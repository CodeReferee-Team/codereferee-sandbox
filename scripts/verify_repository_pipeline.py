#!/usr/bin/env python3
"""Run one repository deployment and Chaos validation through the HTTP adapter logic."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import RepositoryValidationRequest, validate_repository  # noqa: E402


def main() -> int:
    args = parse_args()
    request = RepositoryValidationRequest.model_validate(
        {
            "repositoryUrl": args.repository_url,
            "branch": args.branch,
            "requestId": args.request_id,
            "chaosMode": args.chaos_mode,
            "deploymentProfile": args.deployment_profile,
        }
    )
    result = validate_repository(request)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")

    observation = result.get("chaos_observation") or {}
    summary = {
        "exitCode": result.get("exitCode"),
        "observationStatus": result.get("observationStatus"),
        "infraError": result.get("infraError"),
        "commitSha": result.get("commitSha"),
        "scenario": result.get("scenario"),
        "recovered": observation.get("recovered"),
        "recoverySeconds": observation.get("recovery_seconds"),
        "metrics": result.get("metrics"),
        "stderr": result.get("stderr"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.get("observationStatus") == "observed" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify repository deployment and Chaos end to end.")
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--chaos-mode", default="litmus_pod_delete")
    parser.add_argument("--deployment-profile", required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
