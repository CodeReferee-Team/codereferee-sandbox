#!/usr/bin/env bash
# bootstrap_litmus.ps1의 POSIX 포팅. 고정 커밋의 Litmus operator/fault 매니페스트를 받아 적용.
set -euo pipefail

CONTEXT="${1:-${CODEREFEREE_KUBECTL_CONTEXT:-}}"

OPERATOR_VERSION="3.29.0"
OPERATOR_COMMIT="97cfc6f1ee73af5f8e6b7f8c01e97b116cccfc0c"
FAULT_VERSION="3.30.0"
FAULT_COMMIT="3d485c7854aeabcf136118ff54b78434a1a14099"

command -v kubectl >/dev/null 2>&1 || { echo "kubectl was not found in PATH." >&2; exit 1; }
[ -n "$CONTEXT" ] || CONTEXT="$(kubectl config current-context | tr -d '[:space:]')"
[[ "$CONTEXT" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Invalid Kubernetes context: $CONTEXT" >&2; exit 1; }

kubectl --context "$CONTEXT" get nodes

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST_DIR="$ROOT/.runtime/litmus"
OPERATOR_MANIFEST="$MANIFEST_DIR/litmus-operator-v$OPERATOR_VERSION.yaml"
POD_DELETE_MANIFEST="$MANIFEST_DIR/pod-delete-v$FAULT_VERSION.yaml"
CONTAINER_KILL_MANIFEST="$MANIFEST_DIR/container-kill-v$FAULT_VERSION.yaml"
RUNNER_RBAC_MANIFEST="$ROOT/k8s/litmus-runner-rbac.yaml"
mkdir -p "$MANIFEST_DIR"

download() {  # url dest
  [ -f "$2" ] || curl -fsSL "$1" -o "$2"
}
download "https://raw.githubusercontent.com/litmuschaos/litmus/$OPERATOR_COMMIT/mkdocs/docs/litmus-operator-v$OPERATOR_VERSION.yaml" "$OPERATOR_MANIFEST"
download "https://raw.githubusercontent.com/litmuschaos/chaos-charts/$FAULT_COMMIT/faults/kubernetes/pod-delete/fault.yaml" "$POD_DELETE_MANIFEST"
download "https://raw.githubusercontent.com/litmuschaos/chaos-charts/$FAULT_COMMIT/faults/kubernetes/container-kill/fault.yaml" "$CONTAINER_KILL_MANIFEST"

grep -q 'name: chaos-operator-ce' "$OPERATOR_MANIFEST" && grep -q "chaos-operator:$OPERATOR_VERSION" "$OPERATOR_MANIFEST" \
  || { echo "Downloaded Litmus operator manifest did not match the pinned version." >&2; exit 1; }
grep -q 'kind: ChaosExperiment' "$POD_DELETE_MANIFEST" && grep -q 'name: pod-delete' "$POD_DELETE_MANIFEST" \
  || { echo "Downloaded Pod Delete manifest is invalid." >&2; exit 1; }
grep -q 'kind: ChaosExperiment' "$CONTAINER_KILL_MANIFEST" && grep -q 'name: container-kill' "$CONTAINER_KILL_MANIFEST" \
  || { echo "Downloaded Container Kill manifest is invalid." >&2; exit 1; }

kubectl --context "$CONTEXT" apply -f "$OPERATOR_MANIFEST"
kubectl --context "$CONTEXT" -n litmus rollout status deployment/chaos-operator-ce --timeout=180s
kubectl --context "$CONTEXT" apply -f "$RUNNER_RBAC_MANIFEST"
kubectl --context "$CONTEXT" -n litmus apply -f "$POD_DELETE_MANIFEST"
kubectl --context "$CONTEXT" -n litmus apply -f "$CONTAINER_KILL_MANIFEST"

kubectl --context "$CONTEXT" -n litmus get pods
kubectl --context "$CONTEXT" -n litmus get chaosexperiments
