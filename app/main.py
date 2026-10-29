"""HTTP adapter for fixture and deployed-repository Chaos v1 experiments."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4
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
SMOKE_SCRIPT = PROJECT_ROOT / 'scripts' / 'run_service_smoke.py'
EXPERIMENT_TIMEOUT_SECONDS = 300
DEPLOY_TIMEOUT_SECONDS = 1200
SMOKE_TIMEOUT_SECONDS = 180
# Namespace and image cleanup run after the budget ends; keep this much back.
CLEANUP_RESERVE_SECONDS = 90
# Measured per-scenario durations without deploy, small Python repository,
# kind on one VM (scenario-tests/REPORT.md, 2026-10-19). A scenario in a suite
# is not started when the remaining budget cannot hold it. Unmeasured ones use
# the per-scenario ceiling.
SCENARIO_ESTIMATE_SECONDS = {
    'litmus_container_kill': 130, 'litmus_pod_delete': 140, 'litmus_pod_cpu_hog': 115,
    'litmus_pod_memory_hog': 100, 'deployment_scale_down': 60, 'rollout_restart': 60,
    'service_selector_blackhole': 30,
}
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
    probe_path: str = Field(default='/', alias='probePath')


class RepositoryExecutionFailure(Exception):
    def __init__(self, result: dict):
        self.result = result


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
    pods_affected_count: int | None = Field(default=None, alias="podsAffectedCount", strict=True, ge=1, le=16)
    # Seconds the caller will wait for this response. Without it each stage
    # keeps its own ceiling, as before.
    deadline_seconds: int | None = Field(default=None, alias="deadlineSeconds", ge=60, le=7200)


class Budget:
    """One deadline shared by every stage of a request."""

    def __init__(self, deadline_seconds: int | None, started: float):
        self.started = started
        self.limit = None if deadline_seconds is None else max(0, deadline_seconds - CLEANUP_RESERVE_SECONDS)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining(self) -> float | None:
        return None if self.limit is None else self.limit - self.elapsed()

    def timeout_for(self, stage_limit: int) -> tuple[float, bool]:
        """Seconds to allow and whether the request budget, not the stage, is the bound."""
        remaining = self.remaining()
        if remaining is None or remaining >= stage_limit:
            return stage_limit, False
        return max(1.0, remaining), True


class StageTimeout(Exception):
    def __init__(self, stage: str, seconds: float, budget_bound: bool = False):
        super().__init__(f'{stage} exceeded {round(seconds)} seconds'
                         + (' (request deadline)' if budget_bound else ''))
        self.stage = stage
        self.seconds = seconds
        self.budget_bound = budget_bound


def run_bounded(command: list[str], *, stage: str, seconds: float, budget_bound: bool = False,
                env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """subprocess.run whose timeout also kills grandchildren.

    subprocess.run(timeout=) kills only the direct child. docker build and
    kubectl kept running after it and fought the next request for the node.
    """
    environment = dict(env or os.environ, PYTHONIOENCODING='utf-8')
    process = subprocess.Popen(command, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', errors='replace', env=environment,
                               **({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'
                                  else {'start_new_session': True}))
    try:
        stdout, stderr = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               capture_output=True, timeout=15)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise StageTimeout(stage, seconds, budget_bound) from None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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
    if request.pods_affected_count is not None and not any(mode.startswith('litmus_') for mode in modes or []):
        raise HTTPException(status_code=422, detail='podsAffectedCount requires a Litmus scenario; it does not limit scale/routing/dependency faults.')
    if not experiment_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A Chaos v1 experiment is already running.")

    request_started = time.monotonic()
    budget = Budget(request.deadline_seconds, request_started)
    stage = 'deploy'
    metrics_session = None
    try:
        deployed: dict[str, Any] | None = None
        deployed_target: ChaosTarget | None = None
        if request.deployment_profile or (request.chaos_mode != 'fixture' and request.chaos_target is None):
            if not request.request_id:
                request = request.model_copy(update={'request_id': str(uuid4())})
            deployed = deploy_repository(request, budget)
            deployed_target = ChaosTarget.model_validate(deployed["target"])
        metrics_target = deployed_target or request.chaos_target
        require_declared_dependencies(modes or [], metrics_target)
        if metrics_target is not None:
            from scripts.metrics_lifecycle import RequestMetrics
            metrics_session = RequestMetrics(request.request_id or str(uuid4()), metrics_target.model_dump(by_alias=True))
            metrics_session.start()
        if request.chaos_mode is None and deployed_target is not None:
            stage = 'smoke'
            command = [sys.executable, str(SMOKE_SCRIPT), '--namespace', deployed_target.namespace,
                       '--service', deployed_target.service, '--service-port', str(deployed_target.service_port),
                       '--probe-path', deployed_target.probe_path]
            seconds, bound = budget.timeout_for(SMOKE_TIMEOUT_SECONDS)
            completed = run_bounded(command, stage=stage, seconds=seconds, budget_bound=bound)
            result = parse_experiment_result(completed)
            attach_repository_checks(result, deployed)
            result.update(requestId=request.request_id, repositoryUrl=request.repository_url,
                          commitSha=deployed['repository']['commitSha'], deployment=deployed)
            return result
        scenario_results = []
        planned = modes or [request.chaos_mode]
        suite = bool(modes) and (len(modes) > 1 or request.chaos_mode.startswith('suite_'))
        for mode in planned:
            stage = f'scenario:{mode}'
            scenario_request = request.model_copy(update={'chaos_mode': mode})
            command = experiment_command(scenario_request, deployed_target)
            remaining = budget.remaining()
            needed = SCENARIO_ESTIMATE_SECONDS.get(mode, EXPERIMENT_TIMEOUT_SECONDS)
            if suite and remaining is not None and remaining < needed:
                scenario_results.append(skipped_scenario(
                    mode, 'sandbox_budget_exceeded',
                    f'{max(0, round(remaining))}s left of the request deadline; this scenario needs about {needed}s.',
                    timed_out=False))
                continue
            seconds, bound = budget.timeout_for(EXPERIMENT_TIMEOUT_SECONDS)
            try:
                completed = run_bounded(command, stage=stage, seconds=seconds, budget_bound=bound)
            except StageTimeout as exc:
                if not suite:
                    raise
                scenario_results.append(skipped_scenario(mode, timeout_code(exc), str(exc)))
                continue
            result = parse_experiment_result(completed)
            result['chaosMode'] = mode
            scenario_results.append(result)
            # A scenario the infrastructure could not run says nothing about the
            # next one, so the suite goes on. A target that did not recover makes
            # every later baseline unhealthy, so the suite stops there.
            if result.get('observationStatus') != 'infrastructure_error' and result.get('exitCode') != 0:
                break
        if suite:
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
            attach_repository_checks(result, deployed)
        return result
    except RepositoryExecutionFailure as exc:
        result = exc.result
        return result
    except HTTPException:
        raise
    except StageTimeout as exc:
        result = infrastructure_error(str(exc), request, timed_out=True, stage=exc.stage, code=timeout_code(exc))
        result['budget'] = {'stage': exc.stage, 'elapsedSeconds': round(budget.elapsed(), 2),
                            'limitSeconds': budget.limit if exc.budget_bound else exc.seconds,
                            'elapsedScope': 'request', 'limitScope': 'request' if exc.budget_bound else 'stage'}
        result.setdefault('source', {})['execution_limit'] = dict(result['budget'],
            validationSupport='unsupported_within_budget')
        return result
    except (RuntimeError, KeyError, json.JSONDecodeError) as exc:
        result = infrastructure_error(str(exc), request, timed_out=False, stage=stage)
        return result
    finally:
        if metrics_session is not None:
            metrics_report = metrics_session.finish()
            if 'result' in locals():
                result.setdefault('source', {})['metrics_observation'] = metrics_report
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
        if 'result' in locals():
            result['durationMillis'] = round((time.monotonic() - request_started) * 1000)
        experiment_lock.release()


def attach_repository_checks(result: dict, deployed: dict) -> None:
    """Preserve actual build/test evidence without changing AI judgement policy."""
    if 'workspaceCleanup' in deployed:
        result.setdefault('source', {})['workspace_cleanup'] = deployed['workspaceCleanup']
    checks = deployed.get('sandboxReport')
    if not isinstance(checks, dict):
        return
    report = result.setdefault('sandboxReport', {})
    report.update({key: checks[key] for key in ('detected_stack', 'verification_declared',
        'test_execution', 'test_count', 'steps', 'check_cache_cleanup') if key in checks})
    report['repository_check_outcome'] = checks.get('outcome')


def aggregate_scenarios(mode: str, planned: list[str], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-scenario results into one suite result.

    Scenarios the infrastructure could not run are listed with the reason, and
    the scenarios that did run are judged on their own. Only when none ran is
    the suite an infrastructure error.
    """
    observed = [r for r in results if r.get('observationStatus') == 'observed']
    stopped = [stopped_entry(r) for r in results if r.get('observationStatus') != 'observed']
    ran = {r.get('chaosMode') for r in results}
    not_executed = [m for m in planned if m not in ran]
    first_infra = next((r for r in results if r.get('observationStatus') != 'observed'), None)
    none_observed = not observed
    recovered = bool(observed) and all(r.get('exitCode') == 0 for r in observed)
    count = sum(r.get('metrics', {}).get('probe_count', 0) for r in observed)
    successes = sum(r.get('metrics', {}).get('success_count', 0) for r in observed)
    recovery = [r.get('chaos_observation', {}).get('recovery_seconds') for r in observed]
    recovery = [v for v in recovery if isinstance(v, (int, float))]
    source = observed[0] if observed else (results[0] if results else {})
    return {'schemaVersion': 'chaos-v1', 'scenario': mode,
            'observationStatus': 'infrastructure_error' if none_observed else 'observed',
            'exitCode': None if none_observed else (0 if recovered else 1),
            'infraError': (first_infra or {}).get('infraError') if none_observed else None,
            'stderr': (first_infra or {}).get('stderr', '') if none_observed else '',
            'stoppedAt': f"scenario:{first_infra.get('chaosMode')}" if none_observed and first_infra else None,
            'timedOut': any(r.get('timedOut', False) for r in results),
            'baseline': source.get('baseline', {}),
            'metrics': {'availability': successes/count if count else None,
                        'error_rate': 1-successes/count if count else None,
                        'probe_count': count, 'success_count': successes, 'failure_count': count-successes,
                        'p95_latency_ms': None, 'error_rate_denominator': 'sum of per-scenario HTTP probe counts of observed scenarios; inspect individual scenarios for latency'},
            'chaos_observation': {'type': 'scenario_suite', 'recovered': recovered if observed else None,
                # The longest outage is what the recovery bound compares against.
                'recovery_seconds': max(recovery) if recovery else None,
                # The error budget is spent by every outage, so it uses the sum.
                'total_downtime_seconds': round(sum(recovery), 2) if recovery else None,
                'target_configuration': source.get('chaos_observation', {}).get('target_configuration', {}),
                'abort_condition': {'triggered': none_observed and bool(results),
                                    'reason': (first_infra or {}).get('infraError') if none_observed else None},
                'scenarios': results, 'requested': planned,
                'executed': [r.get('chaosMode') for r in observed],
                'stopped': stopped, 'not_executed': not_executed,
                'partial': bool(observed) and bool(stopped or not_executed)},
            'source': {'real_execution_observed': not none_observed}}


def stopped_entry(result: dict[str, Any]) -> dict[str, Any]:
    """Where a scenario stopped and why, short enough for a report line."""
    reason = litmus_failure_reason(result)
    if reason is None:
        lines = (result.get('stderr') or '').strip().splitlines()
        reason = lines[-1] if lines else None
    return {'chaosMode': result.get('chaosMode'), 'infraError': result.get('infraError'),
            'timedOut': bool(result.get('timedOut')), 'reason': reason[:300] if reason else None}


def litmus_failure_reason(result: dict[str, Any]) -> str | None:
    """Litmus puts the helper's error in errorOutput.reason, often as nested JSON.

    The runner's stderr only says the experiment failed. M10 stopped on
    "failed to create tc rules: Error: Specified qdisc kind is unknown." and
    that sentence was only here.
    """
    output = (((result.get('chaosResult') or {}).get('raw') or {}).get('experimentStatus') or {}).get('errorOutput') or {}
    reason, code = output.get('reason'), output.get('errorCode')
    if not reason:
        return None
    try:
        nested = json.loads(reason)
    except (TypeError, ValueError):
        nested = None
    if isinstance(nested, dict):
        reason = nested.get('reason') or reason
        phase = nested.get('phase')
        code = ' '.join(str(x) for x in (code, phase) if x)
    text = ' '.join(str(reason).replace(' :', '').split())
    return f'{code}: {text}' if code else text


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


DEPENDENCY_SCENARIOS = {'dependency_database_outage': 'database', 'dependency_redis_outage': 'redis'}


def require_declared_dependencies(modes: list[str], target: ChaosTarget | None) -> None:
    # The profile is only known after deploy, but checking here, before the first
    # fault, keeps a suite from running every earlier scenario only to be refused.
    for mode in modes:
        dependency = DEPENDENCY_SCENARIOS.get(mode)
        if dependency and target is not None and not target.dependencies.get(dependency):
            raise HTTPException(status_code=422, detail=f'Profile must declare the {dependency} dependency and a business probe.')


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
            '--probe-path', target.probe_path,
        ]
    if request.chaos_mode == "service_selector_blackhole" and target is not None:
        return [
            sys.executable, str(SERVICE_SELECTOR_SCRIPT),
            "--namespace", target.namespace,
            "--deployment", target.deployment,
            "--service", target.service,
            "--service-port", str(target.service_port),
            "--label-selector", target.label_selector,
            '--probe-path', target.probe_path,
        ]
    if request.chaos_mode == "rollout_restart" and target is not None:
        return [
            sys.executable, str(ROLLOUT_RESTART_SCRIPT),
            "--namespace", target.namespace,
            "--deployment", target.deployment,
            "--service", target.service,
            "--service-port", str(target.service_port),
            "--label-selector", target.label_selector,
            '--probe-path', target.probe_path,
        ]
    scenario_by_mode = {f'litmus_{scenario}': scenario for scenario in
                        ('pod_delete', 'container_kill', 'pod_cpu_hog', 'pod_network_latency', 'pod_network_loss', 'pod_memory_hog', 'pod_memory_oom')}
    scenario = scenario_by_mode.get(request.chaos_mode or "")
    if scenario is None or target is None:
        raise HTTPException(
            status_code=422,
            detail="litmus_pod_delete requires chaosTarget; fixture mode does not accept a target.",
        )
    command = [
        sys.executable, str(LITMUS_SCRIPT),
        "--namespace", target.namespace,
        "--deployment", target.deployment,
        "--service", target.service,
        "--service-port", str(target.service_port),
        "--label-selector", target.label_selector,
        '--probe-path', target.probe_path,
        "--scenario", scenario,
        "--baseline-probes", "20",
        "--timeout-seconds", "240",
    ]
    if request.pods_affected_count is not None:
        command.extend(['--pods-affected-count', str(request.pods_affected_count)])
    return command


def deploy_repository(request: RepositoryValidationRequest, budget: Budget | None = None) -> dict[str, Any]:
    if request.chaos_mode is not None and request.chaos_mode not in SCENARIOS and request.chaos_mode not in SUITES and not (request.chaos_mode or '').startswith('suite_custom__'):
        raise HTTPException(status_code=422, detail="deploymentProfile requires a supported chaosMode.")
    if not request.request_id:
        raise HTTPException(status_code=422, detail="deploymentProfile requires requestId.")
    command = [sys.executable, str(DEPLOY_SCRIPT), '--repository-url', request.repository_url,
               '--request-id=' + request.request_id,
               *(['--profile', request.deployment_profile] if request.deployment_profile else []),
               *(['--branch', request.branch] if request.branch else []),
               *(['--commit-sha', request.commit_sha] if request.commit_sha else [])]
    patch_path: Path | None = None
    if request.patch_diff is not None:
        if len(request.patch_diff.encode("utf-8")) > 1_000_000:
            raise HTTPException(status_code=422, detail="patchDiff exceeds the 1 MiB Sandbox limit.")
        runtime = PROJECT_ROOT / ".runtime"
        runtime.mkdir(exist_ok=True)
        # requestId is external metadata, not a filesystem path.
        patch_path = runtime / f"patch-{uuid4().hex}.diff"
        # Preserve diff bytes across Windows/Linux. Platform newline translation
        # adds CR to LF context lines and makes git apply reject an LF checkout.
        patch_path.write_text(request.patch_diff, encoding="utf-8", newline="\n")
        command.extend(["--patch-file", str(patch_path)])
    try:
        seconds, bound = (budget or Budget(None, time.monotonic())).timeout_for(DEPLOY_TIMEOUT_SECONDS)
        completed = run_bounded(command, stage='deploy', seconds=seconds, budget_bound=bound)
    finally:
        if patch_path:
            patch_path.unlink(missing_ok=True)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = None
    if payload and payload.get('executionFailure'):
        raise RepositoryExecutionFailure(payload['result'])
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


def timeout_code(exc: StageTimeout) -> str:
    return 'sandbox_budget_exceeded' if exc.budget_bound else 'sandbox_execution_timeout'


def skipped_scenario(mode: str, code: str, reason: str, *, timed_out: bool = True) -> dict[str, Any]:
    """A scenario that produced no observation: it timed out or was not started."""
    return {'schemaVersion': 'chaos-v1', 'chaosMode': mode, 'observationStatus': 'infrastructure_error',
            'exitCode': None, 'infraError': code, 'stderr': reason, 'timedOut': timed_out}


def infrastructure_error(
    message: str, request: RepositoryValidationRequest, *, timed_out: bool, stage: str | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    return {
        "exitCode": None,
        "stdout": "",
        "stderr": message,
        "timedOut": timed_out,
        "failureKind": "execution_limit" if timed_out else "infrastructure",
        "failurePhase": stage,
        "validationSupport": "unsupported_within_budget" if timed_out else None,
        # Where the request stopped: deploy, smoke, or scenario:<chaosMode>.
        "stoppedAt": stage,
        "durationMillis": 0,
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
        "infraError": code or ("sandbox_execution_timeout" if timed_out else "sandbox_execution_failed"),
        "requestId": request.request_id,
        "repositoryUrl": request.repository_url,
        "branch": request.branch,
        "commitSha": request.commit_sha,
    }
