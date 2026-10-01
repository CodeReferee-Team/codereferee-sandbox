"""HTTP adapter for fixture and deployed-repository Chaos v1 experiments."""

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
LITMUS_SCRIPT = PROJECT_ROOT / "scripts" / "run_litmus_pod_delete.py"
EXPERIMENT_TIMEOUT_SECONDS = 300
experiment_lock = threading.Lock()

app = FastAPI(title="CodeReferee Sandbox", version="0.1.0")


class ChaosTarget(BaseModel):
    """A pre-deployed, request-scoped Kubernetes workload to observe."""

    model_config = ConfigDict(populate_by_name=True)

    namespace: str
    deployment: str
    service: str
    service_port: int = Field(alias="servicePort")
    label_selector: str = Field(alias="labelSelector")
    name: str | None = None


class RepositoryValidationRequest(BaseModel):
    """Accept the existing AI Core request names in camelCase or snake_case."""

    model_config = ConfigDict(populate_by_name=True)

    repository_url: str = Field(alias="repositoryUrl")
    branch: str | None = None
    commit_sha: str | None = Field(default=None, alias="commitSha")
    request_id: str | None = Field(default=None, alias="requestId")
    chaos_mode: str | None = Field(default=None, alias="chaosMode")
    chaos_target: ChaosTarget | None = Field(default=None, alias="chaosTarget")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/repositories/validate")
def validate_repository(request: RepositoryValidationRequest) -> dict[str, Any]:
    """Run fixture Chaos, or explicit Litmus Pod Delete for a deployed target."""

    if not experiment_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A Chaos v1 experiment is already running.")

    try:
        command = experiment_command(request)
        completed = subprocess.run(
            command,
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


def experiment_command(request: RepositoryValidationRequest) -> list[str]:
    if request.chaos_mode in (None, "fixture") and request.chaos_target is None:
        return [sys.executable, str(EXPERIMENT_SCRIPT)]
    if request.chaos_mode != "litmus_pod_delete" or request.chaos_target is None:
        raise HTTPException(
            status_code=422,
            detail="litmus_pod_delete requires chaosTarget; fixture mode does not accept a target.",
        )
    target = request.chaos_target
    return [
        sys.executable, str(LITMUS_SCRIPT),
        "--namespace", target.namespace,
        "--deployment", target.deployment,
        "--service", target.service,
        "--service-port", str(target.service_port),
        "--label-selector", target.label_selector,
        "--baseline-probes", "20",
        "--timeout-seconds", "240",
    ]


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
