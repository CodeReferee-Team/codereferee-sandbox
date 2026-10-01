"""HTTP adapter for the controlled Chaos v1 fixture experiment."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_SCRIPT = PROJECT_ROOT / "scripts" / "run_pod_kill_experiment.py"
EXPERIMENT_TIMEOUT_SECONDS = 120
experiment_lock = threading.Lock()

app = FastAPI(title="CodeReferee Sandbox", version="0.1.0")


class RepositoryValidationRequest(BaseModel):
    """Accept the existing AI Core request names in camelCase or snake_case."""

    model_config = ConfigDict(populate_by_name=True)

    repository_url: str = Field(alias="repositoryUrl")
    branch: str | None = None
    commit_sha: str | None = Field(default=None, alias="commitSha")
    request_id: str | None = Field(default=None, alias="requestId")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/repositories/validate")
def validate_repository(request: RepositoryValidationRequest) -> dict[str, Any]:
    """Run the controlled Chaos v1 experiment.

    The request keeps the AI Core repository fields for API compatibility. Chaos v1
    intentionally executes the local fixture service rather than the requested
    repository; see docs/chaos-v1-contract.md for the scope.
    """

    if not experiment_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A Chaos v1 experiment is already running.")

    try:
        completed = subprocess.run(
            [sys.executable, str(EXPERIMENT_SCRIPT)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=EXPERIMENT_TIMEOUT_SECONDS,
        )
        result = parse_experiment_result(completed)
        # Chaos v1 performs HTTP probes through kubectl port-forward, but it
        # does not start a headless browser.  Make that distinction explicit
        # so AI Core does not infer a failed browser smoke check from
        # browserLoaded=false.
        result["serviceCheckAttempted"] = result.get("observationStatus") == "observed"
        result["browserCheckAttempted"] = False
        result["requestId"] = request.request_id
        result["repositoryUrl"] = request.repository_url
        result["branch"] = request.branch
        result["commitSha"] = request.commit_sha
        return result
    except subprocess.TimeoutExpired as exc:
        return infrastructure_error(
            f"Chaos v1 experiment exceeded {EXPERIMENT_TIMEOUT_SECONDS} seconds: {exc}",
            request,
            timed_out=True,
        )
    finally:
        experiment_lock.release()


def parse_experiment_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "exitCode": None,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip() or "Sandbox experiment returned invalid JSON.",
            "timedOut": False,
            "durationMillis": 0,
            "serverStarted": False,
            "serverUrl": None,
            "httpStatus": None,
            "browserLoaded": False,
            "pageTitle": None,
            "runCommand": [sys.executable, str(EXPERIMENT_SCRIPT)],
            "schemaVersion": "chaos-v1",
            "observationStatus": "infrastructure_error",
            "infraError": "sandbox_process_failed",
        }
    if completed.returncode != 0 and not result.get("stderr"):
        result["stderr"] = completed.stderr.strip() or "Chaos v1 experiment failed."
    return result


def infrastructure_error(
    message: str, request: RepositoryValidationRequest, *, timed_out: bool
) -> dict[str, Any]:
    return {
        "exitCode": None,
        "stdout": "",
        "stderr": message,
        "timedOut": timed_out,
        "durationMillis": EXPERIMENT_TIMEOUT_SECONDS * 1000 if timed_out else 0,
        "serverStarted": False,
        "serverUrl": None,
        "httpStatus": None,
        "browserLoaded": False,
        "serviceCheckAttempted": False,
        "browserCheckAttempted": False,
        "pageTitle": None,
        "runCommand": [sys.executable, str(EXPERIMENT_SCRIPT)],
        "schemaVersion": "chaos-v1",
        "observationStatus": "infrastructure_error",
        "infraError": "sandbox_execution_timeout" if timed_out else "sandbox_execution_failed",
        "requestId": request.request_id,
        "repositoryUrl": request.repository_url,
        "branch": request.branch,
        "commitSha": request.commit_sha,
    }
