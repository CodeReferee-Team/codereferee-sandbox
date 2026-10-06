"""HTTP adapter for fixture and deployed-repository Chaos v1 experiments."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app.scenarios import ALIASES, SCENARIOS, SUITES, resolve_scenarios


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_SCRIPT = PROJECT_ROOT / "scripts" / "run_pod_kill_experiment.py"
LITMUS_SCRIPT = PROJECT_ROOT / "scripts" / "run_litmus_pod_delete.py"
SCALE_SCRIPT = PROJECT_ROOT / "scripts" / "run_deployment_scale_experiment.py"
SERVICE_SELECTOR_SCRIPT = PROJECT_ROOT / "scripts" / "run_service_selector_experiment.py"
ROLLOUT_RESTART_SCRIPT = PROJECT_ROOT / "scripts" / "run_rollout_restart_experiment.py"
DEPENDENCY_SCRIPT = PROJECT_ROOT / 'scripts' / 'run_dependency_experiment.py'
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts" / "deploy_repository.py"
EXPERIMENT_TIMEOUT_SECONDS = 300
experiment_lock = threading.Lock()

app = FastAPI(title="CodeReferee Sandbox", version="0.1.0")


def kubectl_command(*args: str) -> list[str]:
    """Use the request runtime's explicit cluster context when configured."""
    context = os.getenv("CODEREFEREE_KUBECTL_CONTEXT")
    if not context and os.getenv("CODEREFEREE_CLUSTER_PROVIDER", "existing").lower() == "kind":
        context = f"kind-{os.getenv('CODEREFEREE_KIND_CLUSTER_NAME', 'codereferee')}"
    command = ["kubectl"]
    if context:
        command.extend(["--context", context])
    command.extend(args)
    return command


class ChaosTarget(BaseModel):
    """A pre-deployed, request-scoped Kubernetes workload to observe."""

    model_config = ConfigDict(populate_by_name=True)

    namespace: str
    deployment: str
    service: str
    service_port: int = Field(alias="servicePort")
    label_selector: str = Field(alias="labelSelector")
    name: str | None = None
    dependencies: dict[str, Any] = Field(default_factory=dict)


class RepositoryValidationRequest(BaseModel):
    """Accept the existing AI Core request names in camelCase or snake_case."""

    model_config = ConfigDict(populate_by_name=True)

    repository_url: str = Field(alias="repositoryUrl")
    branch: str | None = None
    commit_sha: str | None = Field(default=None, alias="commitSha")
    request_id: str | None = Field(default=None, alias="requestId")
    chaos_mode: str | None = Field(default=None, alias="chaosMode")
    chaos_target: ChaosTarget | None = Field(default=None, alias="chaosTarget")
    deployment_profile: str | None = Field(default=None, alias="deploymentProfile")
    patch_diff: str | None = Field(default=None, alias="patchDiff")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get('/chaos/scenarios')
def scenario_catalogue() -> dict[str, Any]:
    return {'scenarios': SCENARIOS, 'suites': SUITES, 'aliases': ALIASES,
            'execution': 'serial_per_request', 'customModePrefix': 'suite_custom__'}


@app.post("/repositories/validate")
def validate_repository(request: RepositoryValidationRequest) -> dict[str, Any]:
    """Run fixture Chaos, a pre-deployed target, or a profile-driven repository deployment."""

    modes = None
    if request.chaos_mode and request.chaos_mode != 'fixture':
        try:
            modes = resolve_scenarios(request.chaos_mode)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not experiment_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A Chaos v1 experiment is already running.")

    try:
        deployed: dict[str, Any] | None = None
        deployed_target: ChaosTarget | None = None
        if request.deployment_profile:
            deployed = deploy_repository(request)
            deployed_target = ChaosTarget.model_validate(deployed["target"])
        scenario_results = []
        for mode in modes or [request.chaos_mode]:
            scenario_request = request.model_copy(update={'chaos_mode': mode})
            command = experiment_command(scenario_request, deployed_target)
            child_environment = os.environ.copy()
            child_environment['PYTHONIOENCODING'] = 'utf-8'
            completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True,
                                       text=True, encoding='utf-8', errors='replace',
                                       env=child_environment, timeout=EXPERIMENT_TIMEOUT_SECONDS)
            result = parse_experiment_result(completed)
            result['chaosMode'] = mode
            scenario_results.append(result)
            if result.get('observationStatus') == 'infrastructure_error' or result.get('exitCode') != 0:
                break
        if modes and (len(modes) > 1 or request.chaos_mode.startswith('suite_')):
            result = aggregate_scenarios(request.chaos_mode, modes, scenario_results)
        # Chaos v1 performs HTTP probes through kubectl port-forward, but it
        # does not start a headless browser.  Make that distinction explicit
        # so AI Core does not infer a failed browser smoke check from
        # browserLoaded=false.
        result["serviceCheckAttempted"] = result.get("observationStatus") == "observed"
        result["browserCheckAttempted"] = False
        result["requestId"] = request.request_id
        result["repositoryUrl"] = request.repository_url
        result["branch"] = request.branch
        result["commitSha"] = (deployed or {}).get("repository", {}).get("commitSha") or request.commit_sha
        if deployed:
            result["deployment"] = deployed
        return result
    except HTTPException:
        raise
    except (RuntimeError, KeyError, json.JSONDecodeError) as exc:
        return infrastructure_error(str(exc), request, timed_out=False)
    except subprocess.TimeoutExpired as exc:
        return infrastructure_error(
            f"Chaos v1 experiment exceeded {EXPERIMENT_TIMEOUT_SECONDS} seconds: {exc}",
            request,
            timed_out=True,
        )
    finally:
        if 'deployed_target' in locals() and deployed_target is not None:
            namespace_removed = cleanup_namespace(deployed_target.namespace)
            if deployed and deployed.get('image'):
                from scripts.artifact_cleanup import cleanup_request_image
                try:
                    report = cleanup_request_image(deployed['image'], deployed_target.namespace,
                                                   remove_from_kind=namespace_removed)
                    report['namespace_removed'] = namespace_removed
                except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
                    report = {'namespace_removed': namespace_removed, 'errors': [str(exc)]}
                if 'result' in locals():
                    result.setdefault('source', {})['artifact_cleanup'] = report
        experiment_lock.release()


def aggregate_scenarios(mode: str, planned: list[str], results: list[dict[str, Any]]) -> dict[str, Any]:
    infra = next((r for r in results if r.get('observationStatus') == 'infrastructure_error'), None)
    recovered = len(results) == len(planned) and all(r.get('exitCode') == 0 for r in results)
    count = sum(r.get('metrics', {}).get('probe_count', 0) for r in results)
    successes = sum(r.get('metrics', {}).get('success_count', 0) for r in results)
    recovery = [r.get('chaos_observation', {}).get('recovery_seconds') for r in results]
    recovery = [v for v in recovery if isinstance(v, (int, float))]
    return {'schemaVersion': 'chaos-v1', 'scenario': mode,
            'observationStatus': 'infrastructure_error' if infra else 'observed',
            'exitCode': None if infra else (0 if recovered else 1),
            'infraError': infra.get('infraError') if infra else None,
            'stderr': infra.get('stderr', '') if infra else '',
            'timedOut': any(r.get('timedOut', False) for r in results),
            'baseline': results[0].get('baseline', {}),
            'metrics': {'availability': successes/count if count else None,
                        'error_rate': 1-successes/count if count else None,
                        'probe_count': count, 'success_count': successes, 'failure_count': count-successes,
                        'p95_latency_ms': None, 'error_rate_denominator': 'sum of per-scenario HTTP probe counts; inspect individual scenarios for latency'},
            'chaos_observation': {'type': 'scenario_suite', 'recovered': recovered,
                'recovery_seconds': max(recovery) if recovery else None,
                'target_configuration': results[0].get('chaos_observation', {}).get('target_configuration', {}),
                'abort_condition': {'triggered': bool(infra), 'reason': infra.get('infraError') if infra else None},
                'scenarios': results, 'requested': planned, 'not_executed': planned[len(results):]},
            'source': {'real_execution_observed': not bool(infra)}}


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
    result.setdefault("exitCode", completed.returncode)
    result.setdefault("timedOut", False)
    return result


def experiment_command(request: RepositoryValidationRequest, deployed_target: ChaosTarget | None = None) -> list[str]:
    if request.chaos_mode in (None, "fixture") and request.chaos_target is None:
        return [sys.executable, str(EXPERIMENT_SCRIPT)]
    target = deployed_target or request.chaos_target
    if request.chaos_mode in ('dependency_database_outage', 'dependency_redis_outage') and target is not None:
        dependency = 'database' if request.chaos_mode == 'dependency_database_outage' else 'redis'
        config = target.dependencies.get(dependency)
        if not config:
            raise HTTPException(status_code=422, detail=f'Profile must declare the {dependency} dependency and a business probe.')
        return [sys.executable, str(DEPENDENCY_SCRIPT), '--namespace', target.namespace,
                '--deployment', target.deployment, '--service', target.service,
                '--service-port', str(target.service_port), '--scenario', request.chaos_mode,
                '--dependency-config', json.dumps(config)]
    if request.chaos_mode == "deployment_scale_down" and target is not None:
        return [
            sys.executable, str(SCALE_SCRIPT),
            "--namespace", target.namespace,
            "--deployment", target.deployment,
            "--service", target.service,
            "--service-port", str(target.service_port),
            "--label-selector", target.label_selector,
        ]
    if request.chaos_mode == "service_selector_blackhole" and target is not None:
        return [
            sys.executable, str(SERVICE_SELECTOR_SCRIPT),
            "--namespace", target.namespace,
            "--deployment", target.deployment,
            "--service", target.service,
            "--service-port", str(target.service_port),
            "--label-selector", target.label_selector,
        ]
    if request.chaos_mode == "rollout_restart" and target is not None:
        return [
            sys.executable, str(ROLLOUT_RESTART_SCRIPT),
            "--namespace", target.namespace,
            "--deployment", target.deployment,
            "--service", target.service,
            "--service-port", str(target.service_port),
            "--label-selector", target.label_selector,
        ]
    scenario_by_mode = {f'litmus_{scenario}': scenario for scenario in
                        ('pod_delete', 'container_kill', 'pod_cpu_hog', 'pod_network_latency', 'pod_network_loss', 'pod_memory_hog', 'pod_memory_oom')}
    scenario = scenario_by_mode.get(request.chaos_mode or "")
    if scenario is None or target is None:
        raise HTTPException(
            status_code=422,
            detail="litmus_pod_delete requires chaosTarget; fixture mode does not accept a target.",
        )
    return [
        sys.executable, str(LITMUS_SCRIPT),
        "--namespace", target.namespace,
        "--deployment", target.deployment,
        "--service", target.service,
        "--service-port", str(target.service_port),
        "--label-selector", target.label_selector,
        "--scenario", scenario,
        "--baseline-probes", "20",
        "--timeout-seconds", "240",
    ]


def deploy_repository(request: RepositoryValidationRequest) -> dict[str, Any]:
    if request.chaos_mode not in SCENARIOS and request.chaos_mode not in SUITES and not (request.chaos_mode or '').startswith('suite_custom__'):
        raise HTTPException(status_code=422, detail="deploymentProfile requires a supported chaosMode.")
    if not request.request_id:
        raise HTTPException(status_code=422, detail="deploymentProfile requires requestId.")
    command = [sys.executable, str(DEPLOY_SCRIPT), "--repository-url", request.repository_url,
               "--request-id", request.request_id, "--profile", request.deployment_profile,
               *(["--branch", request.branch] if request.branch else [])]
    patch_path: Path | None = None
    if request.patch_diff is not None:
        if len(request.patch_diff.encode("utf-8")) > 1_000_000:
            raise HTTPException(status_code=422, detail="patchDiff exceeds the 1 MiB Sandbox limit.")
        runtime = PROJECT_ROOT / ".runtime"
        runtime.mkdir(exist_ok=True)
        patch_path = runtime / f"patch-{request.request_id}.diff"
        patch_path.write_text(request.patch_diff, encoding="utf-8")
        command.extend(["--patch-file", str(patch_path)])
    try:
        child_environment = os.environ.copy()
        child_environment['PYTHONIOENCODING'] = 'utf-8'
        completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', env=child_environment, timeout=600)
    finally:
        if patch_path:
            patch_path.unlink(missing_ok=True)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Repository deployment failed.")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Repository deployment returned invalid JSON.") from exc


def cleanup_namespace(namespace: str) -> bool:
    """Best-effort cleanup; evidence has already been returned or recorded."""
    environment = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment.pop(key, None)
    try:
        completed = subprocess.run(kubectl_command("delete", "namespace", namespace, "--ignore-not-found=true", "--wait=true", '--timeout=60s'),
                       cwd=PROJECT_ROOT, capture_output=True, text=True, env=environment, timeout=70)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


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
