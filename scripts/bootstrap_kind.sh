#!/usr/bin/env bash
# bootstrap_kind.ps1의 POSIX 포팅. kind 클러스터를 만들고 CODEREFEREE_* 환경변수를 설정한다.
# 변수를 현재 셸에 남기려면 dot-source 하라:  . ./scripts/bootstrap_kind.sh
set -euo pipefail

CLUSTER_NAME="${1:-codereferee}"
[[ "$CLUSTER_NAME" =~ ^[a-z0-9][a-z0-9.-]*$ ]] || {
  echo "ClusterName must contain only lower-case letters, numbers, dots, and hyphens." >&2; exit 1; }

for cmd in docker kubectl; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "$cmd was not found in PATH." >&2; exit 1; }
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v kind >/dev/null 2>&1; then
  KIND="$(command -v kind)"
elif [ -x "$ROOT/.runtime/tools/kind" ]; then
  KIND="$ROOT/.runtime/tools/kind"
else
  echo "kind was not found. Run ./scripts/install_kind.sh first." >&2; exit 1
fi

docker info >/dev/null
if ! "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  "$KIND" create cluster --name "$CLUSTER_NAME" --wait 180s
fi

CONTEXT="kind-$CLUSTER_NAME"
kubectl --context "$CONTEXT" get nodes

export CODEREFEREE_CLUSTER_PROVIDER="kind"
export CODEREFEREE_KIND_CLUSTER_NAME="$CLUSTER_NAME"
export CODEREFEREE_KUBECTL_CONTEXT="$CONTEXT"
export CODEREFEREE_KIND_COMMAND="$KIND"

echo "kind cluster is ready: $CLUSTER_NAME"
echo "Configured: CODEREFEREE_KUBECTL_CONTEXT=$CONTEXT (dot-source to keep in your shell)"
