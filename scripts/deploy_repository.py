#!/usr/bin/env python3
"""Build and deploy a supported repository into a request-scoped namespace.

This is deliberately plan-driven.  A Git URL tells us where code lives, but it
does not safely describe its database, Redis, ports, or health endpoint.  A
deployment profile supplies those reproducible Sandbox-only details.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from collect_baseline import kubectl_environment


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
    try:
        repository = workspace / "repository"
        clone(args.repository_url, args.branch, repository)
        if args.patch_file:
            apply_patch(repository, args.patch_file)
        profile_name = args.profile or repository_profile_name(repository)
        profile = load_profile(profile_name)
        commit_sha = run(["git", "rev-parse", "HEAD"], cwd=repository).stdout.strip()
        image = f"codereferee/{request}:{commit_sha[:12]}"
        dockerfile = repository / profile.get("dockerfile", "Dockerfile")
        if not dockerfile.is_file():
            raise RuntimeError(f"Required Dockerfile was not found: {profile.get('dockerfile', 'Dockerfile')}")
        build_context = repository / profile.get("buildContext", ".")
        run(["docker", "build", "--tag", image, "--file", str(dockerfile), str(build_context)])
        template = ROOT / profile["manifestTemplate"]
        if not template.is_file():
            raise RuntimeError(f"Deployment template was not found: {template}")
        rendered = render_template(template, namespace, image, profile)
        namespace_may_exist = True
        apply(rendered)
        target = dict(profile["target"])
        target["namespace"] = namespace
        wait_rollout(namespace, target["deployment"], args.rollout_timeout_seconds)
        print(json.dumps({
            "repository": {"url": args.repository_url, "branch": args.branch, "commitSha": commit_sha},
            "target": target,
            "image": image,
            "deploymentProfile": profile_name,
        }, ensure_ascii=False))
        return 0
    except Exception:
        if namespace_may_exist:
            delete_namespace(namespace)
        raise
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy a supported repository for a Chaos run.")
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--branch")
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
    command = ["git", "clone", "--depth", "1"]
    if branch:
        command.extend(["--branch", branch])
    command.extend([url, str(destination)])
    run(command)


def apply_patch(repository: Path, patch_file: Path) -> None:
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
    run(["kubectl", "apply", "-f", "-"], input_text=manifest)


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
    run(["kubectl", "rollout", "status", f"deployment/{deployment}", "-n", namespace, f"--timeout={timeout}s"])


def delete_namespace(namespace: str) -> None:
    subprocess.run(["kubectl", "delete", "namespace", namespace, "--ignore-not-found=true", "--wait=false"],
                   text=True, capture_output=True, env=kubectl_environment())


def run(command: list[str], *, cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    buildx_config = RUNTIME_ROOT / "buildx"
    buildx_config.mkdir(parents=True, exist_ok=True)
    environment = kubectl_environment()
    # Keep Docker Buildx state out of a developer's shared home directory.
    environment["BUILDX_CONFIG"] = str(buildx_config)
    completed = subprocess.run(command, cwd=cwd, input=input_text, text=True, capture_output=True,
                               env=environment)
    if completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise RuntimeError(f"{' '.join(command[:3])}: {message}")
    return completed


if __name__ == "__main__":
    raise SystemExit(main())
