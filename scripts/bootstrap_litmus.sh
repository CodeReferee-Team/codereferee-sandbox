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
FAULTS=(pod-delete container-kill pod-cpu-hog pod-network-latency pod-network-loss pod-memory-hog)
RUNNER_RBAC_MANIFEST="$ROOT/k8s/litmus-runner-rbac.yaml"
mkdir -p "$MANIFEST_DIR"

download() {  # url dest
  [ -f "$2" ] && return 0
  local temporary="$2.download.$$"
  if curl -fsSL "$1" -o "$temporary"; then
    mv -f "$temporary" "$2"
  else
    rm -f "$temporary"
    return 1
  fi
}
download "https://raw.githubusercontent.com/litmuschaos/litmus/$OPERATOR_COMMIT/mkdocs/docs/litmus-operator-v$OPERATOR_VERSION.yaml" "$OPERATOR_MANIFEST"
for fault in "${FAULTS[@]}"; do
  download "https://raw.githubusercontent.com/litmuschaos/chaos-charts/$FAULT_COMMIT/faults/kubernetes/$fault/fault.yaml" "$MANIFEST_DIR/$fault-v$FAULT_VERSION.yaml"
done

grep -q 'name: chaos-operator-ce' "$OPERATOR_MANIFEST" && grep -q "chaos-operator:$OPERATOR_VERSION" "$OPERATOR_MANIFEST" \
  || { echo "Downloaded Litmus operator manifest did not match the pinned version." >&2; exit 1; }
for fault in "${FAULTS[@]}"; do
  manifest="$MANIFEST_DIR/$fault-v$FAULT_VERSION.yaml"
  grep -Eq '^kind:[[:space:]]*ChaosExperiment[[:space:]]*$' "$manifest" \
    && grep -Eq "^[[:space:]]*name:[[:space:]]*$fault[[:space:]]*$" "$manifest" \
    && grep -Eq "go-runner:${FAULT_VERSION//./\\.}([\"'[:space:]]|$)" "$manifest" \
    || { echo "Downloaded fault manifest is invalid or has an unexpected version: $fault" >&2; exit 1; }
done

kubectl --context "$CONTEXT" apply -f "$OPERATOR_MANIFEST"
kubectl --context "$CONTEXT" -n litmus rollout status deployment/chaos-operator-ce --timeout=180s
kubectl --context "$CONTEXT" apply -f "$RUNNER_RBAC_MANIFEST"
for fault in "${FAULTS[@]}"; do
  kubectl --context "$CONTEXT" -n litmus apply -f "$MANIFEST_DIR/$fault-v$FAULT_VERSION.yaml"
done

kubectl --context "$CONTEXT" -n litmus get pods
kubectl --context "$CONTEXT" -n litmus get chaosexperiments
