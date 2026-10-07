#!/usr/bin/env python3
"""Build and deploy a supported repository into a request-scoped namespace.

This is deliberately plan-driven.  A Git URL tells us where code lives, but it
does not safely describe its database, Redis, ports, or health endpoint.  A
deployment profile supplies those reproducible Sandbox-only details.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from collect_baseline import kubectl_command, kubectl_environment, load_image_into_cluster
from artifact_cleanup import cleanup_request_image
from execution_plan import ConfigurationRequired, inside, render_plan, resolve_plan
from repository_checks import CheckFailure, verify_repository


ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"
RUNTIME_ROOT = ROOT / ".runtime"


def main() -> int:
    args = parse_args()
    request = safe_name(args.request_id or "local")
    namespace = f"codereferee-{request}"[:63].rstrip("-")
    RUNTIME_ROOT.mkdir(exist_ok=True)
    workspace = RUNTIME_ROOT / f"repository-{request}"
    if workspace.exists():
        raise RuntimeError(f"A repository workspace already exists for requestId={request}.")
    workspace.mkdir()
    namespace_may_exist = False
    image = None
    stage = 'clone'
    checks = None
    started = time.monotonic()
    try:
        repository = workspace / "repository"
        clone(args.repository_url, args.branch, repository)
        if args.commit_sha:
            if not re.fullmatch(r'[a-f0-9]{40}', args.commit_sha):
                raise ConfigurationRequired('commitSha must be a full 40-character SHA.')
            run(['git', 'fetch', '--depth', '1', 'origin', args.commit_sha], cwd=repository)
            run(['git', 'checkout', '--detach', args.commit_sha], cwd=repository)
        stage = 'patch'
        if args.patch_file:
            apply_patch(repository, args.patch_file)
        commit_sha = run(["git", "rev-parse", "HEAD"], cwd=repository).stdout.strip()
        stage = 'verify'
        checks = verify_repository(repository, workspace / 'check-cache', request)
        stage = 'detect'
        plan = resolve_plan(repository, args.profile)
        profile_name = plan.get('profile')
        profile = load_profile(profile_name) if profile_name else plan
        image = f"codereferee/{request}:{commit_sha[:12]}"
        dockerfile = inside(repository, profile.get('dockerfile', 'Dockerfile'))
        if profile.get('generatedDockerfile'):
            dockerfile = workspace / 'generated.Dockerfile'
            dockerfile.write_text(profile['generatedDockerfile'], encoding='utf-8')
        if not dockerfile.is_file():
            raise RuntimeError(f"Required Dockerfile was not found: {profile.get('dockerfile', 'Dockerfile')}")
        build_context = inside(repository, profile.get('buildContext', '.'))
        stage = 'build'
        run(["docker", "build", "--tag", image, '--label', f'codereferee.request-id={request}', "--file", str(dockerfile), str(build_context)])
        stage = 'prepare'
        load_image_into_cluster(image)
        if profile_name:
            template = ROOT / profile['manifestTemplate']
            if not template.is_file():
                raise RuntimeError(f'Deployment template was not found: {template}')
            rendered = render_template(template, namespace, image, profile)
            target = dict(profile['target'])
        else:
            rendered, target = render_plan(plan, namespace, image)
        namespace_may_exist = True
        stage = 'run'
        apply(rendered)
        target["namespace"] = namespace
        wait_rollout(namespace, target["deployment"], args.rollout_timeout_seconds)
        workspace_cleanup = cleanup_workspace(workspace)
        print(json.dumps({
            "repository": {"url": args.repository_url, "branch": args.branch, "commitSha": commit_sha},
            "target": target,
            "image": image,
            "deploymentProfile": profile_name,
            'sandboxReport': checks,
            'workspaceCleanup': workspace_cleanup,
            'executionPlan': {'source': plan['source'], 'dockerfile': 'generated' if profile.get('generatedDockerfile') else profile.get('dockerfile', 'Dockerfile'),
                              'warnings': plan.get('warnings', []), 'servicePort': target['servicePort']},
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        namespace_removed = not namespace_may_exist
        if namespace_may_exist:
            namespace_removed = delete_namespace(namespace)
        cleanup = {'namespace_removed': namespace_removed}
        if image:
            try:
                cleanup.update(cleanup_request_image(image, namespace, remove_from_kind=namespace_may_exist and namespace_removed))
            except (OSError, subprocess.TimeoutExpired):
                cleanup['errors'] = ['Artifact cleanup failed.']
        cleanup['workspace_cleanup'] = cleanup_workspace(workspace)
        message = str(exc)[-6000:]
        if isinstance(exc, CheckFailure):
            checks = exc.report
            stage = checks['failed_step']
        infra = bool((checks or {}).get('infrastructure_error')) or isinstance(exc, (FileNotFoundError, OSError)) or stage == 'prepare' or any(
            term in message.lower() for term in ('cannot connect to the docker daemon', 'dockerdesktoplinuxengine', 'error during connect'))
        code = None if infra else (89 if isinstance(exc, ConfigurationRequired) else
            (checks or {}).get('exit_code', 1))
        report = dict(checks or {}, schema_version='sandbox-result.v1', outcome='failed', failed_step=stage,
            configuration_required=isinstance(exc, ConfigurationRequired))
        report.setdefault('verification_declared', False)
        print(json.dumps({'executionFailure': True, 'result': {
            'schemaVersion': 'sandbox-result.v1', 'observationStatus': 'infrastructure_error' if infra else 'observed',
            'infraError': 'execution_infrastructure_error' if infra else None, 'exitCode': code,
            'timedOut': isinstance(exc, subprocess.TimeoutExpired) or bool((checks or {}).get('timed_out')), 'durationMillis': round((time.monotonic()-started)*1000),
            'stdout': '', 'stderr': message, 'serverStarted': False, 'serviceCheckAttempted': stage == 'run',
            'browserCheckAttempted': False, 'requestId': args.request_id,
            'sandboxReport': report,
            'source': {'artifact_cleanup': cleanup, 'workspace_cleanup': cleanup['workspace_cleanup']}}}, ensure_ascii=False))
        return 1
    finally:
        if workspace.exists():
            cleanup_workspace(workspace)


def cleanup_workspace(workspace: Path) -> dict:
    """Delete only the exact request scratch directory, including readonly Git files."""
    runtime = RUNTIME_ROOT.resolve()
    resolved = workspace.resolve()
    if resolved.parent != runtime or not re.fullmatch(r'repository-[a-z0-9-]{1,45}', resolved.name):
        raise ValueError('Refusing cleanup outside an exact request workspace.')

    def retry_readonly(function, path, exc_info):
        candidate = Path(path)
        if not candidate.resolve().is_relative_to(resolved):
            raise ValueError('Refusing permissions change outside request workspace.')
        os.chmod(candidate, candidate.stat().st_mode | stat.S_IWRITE)
        function(path)

    try:
        if workspace.exists():
            shutil.rmtree(workspace, onerror=retry_readonly)
        return {'removed': not workspace.exists(), 'error': None}
    except (OSError, ValueError) as exc:
        return {'removed': False, 'error': str(exc)[-1000:]}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy a supported repository for a Chaos run.")
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--branch")
    parser.add_argument('--commit-sha')
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--profile", help="Optional checked-in Sandbox deployment profile override.")
    parser.add_argument("--patch-file", type=Path)
    parser.add_argument("--rollout-timeout-seconds", type=int, default=240)
    return parser.parse_args()


def load_profile(name: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise RuntimeError("Invalid deployment profile name.")
    path = PROFILES / f"{name}.json"
    if not path.is_file():
        raise RuntimeError(f"Unsupported deployment profile: {name}")
    profile = json.loads(path.read_text(encoding="utf-8"))
    for key in ("manifestTemplate", "target"):
        if key not in profile:
            raise RuntimeError(f"Deployment profile is missing {key}.")
    return profile


def repository_profile_name(repository: Path) -> str:
    path = repository / ".codereferee" / "validation.yaml"
    if not path.is_file():
        raise RuntimeError("No deployment profile supplied and .codereferee/validation.yaml is missing.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    name = data.get("deploymentProfile") if isinstance(data, dict) else None
    if not isinstance(name, str):
        raise RuntimeError("validation.yaml requires deploymentProfile.")
    return name


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    if not cleaned:
        raise RuntimeError("requestId must include a letter or number.")
    return cleaned[:45]


def clone(url: str, branch: str | None, destination: Path) -> None:
    if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', url) or '..' in url.split('/')[-2:]:
        raise ConfigurationRequired('Only public GitHub HTTPS repository URLs without credentials are supported.')
    # Container builds need repository line endings, not the Windows user's
    # core.autocrlf=true checkout conversion (which breaks gradlew in Linux).
    command = ["git", "-c", "core.autocrlf=false", "clone", "--depth", "1"]
    if branch:
        command.extend(["--branch", branch])
    command.extend([url, str(destination)])
    run(command)


def apply_patch(repository: Path, patch_file: Path) -> None:
    patch_file = patch_file.resolve()
    if not patch_file.is_file():
        raise RuntimeError("Patch file was not found.")
    if patch_file.stat().st_size > 1_000_000:
        raise RuntimeError("Patch exceeds the 1 MiB Sandbox limit.")
    patch = patch_file.read_text(encoding="utf-8")
    if any(value in patch for value in (".github/workflows/", ".git/", "../")):
        raise RuntimeError("Patch changes a protected path.")
    run(["git", "apply", "--check", str(patch_file)], cwd=repository)
    run(["git", "apply", str(patch_file)], cwd=repository)


def apply(manifest: str) -> None:
    run(kubectl_command("apply", "-f", "-"), input_text=manifest)


def render_template(template: Path, namespace: str, image: str, profile: dict[str, Any]) -> str:
    """Render only Sandbox-owned placeholders; never reuse a repository secret."""
    api_replicas = profile.get("apiReplicas", 1)
    if not isinstance(api_replicas, int) or api_replicas < 1:
        raise RuntimeError("Profile apiReplicas must be a positive integer.")
    values = {
        "${NAMESPACE}": namespace,
        "${IMAGE}": image,
        "${API_REPLICAS}": str(api_replicas),
        "${DB_PASSWORD}": secrets.token_hex(24),
        "${BOOTSTRAP_PASSWORD}": secrets.token_hex(24),
    }
    rendered = template.read_text(encoding="utf-8")
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def wait_rollout(namespace: str, deployment: str, timeout: int) -> None:
    run(kubectl_command("rollout", "status", f"deployment/{deployment}", "-n", namespace, f"--timeout={timeout}s"))


def delete_namespace(namespace: str) -> bool:
    try:
        result = subprocess.run(kubectl_command("delete", "namespace", namespace, "--ignore-not-found=true", '--wait=true', '--timeout=60s'),
                       text=True, capture_output=True, env=kubectl_environment(), timeout=70)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run(command: list[str], *, cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    buildx_config = RUNTIME_ROOT / "buildx"
    buildx_config.mkdir(parents=True, exist_ok=True)
    environment = kubectl_environment()
    # Keep Docker Buildx state out of a developer's shared home directory.
    environment["BUILDX_CONFIG"] = str(buildx_config)
    completed = subprocess.run(command, cwd=cwd, input=input_text, text=True, capture_output=True,
                               env=environment, timeout=600)
    if completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise RuntimeError(f"{' '.join(command[:3])}: {message}")
    return completed


if __name__ == "__main__":
    raise SystemExit(main())
