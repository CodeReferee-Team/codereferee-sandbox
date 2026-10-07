#!/usr/bin/env bash
# bootstrap_local.ps1의 POSIX 포팅. kind 설치(필요 시) → 클러스터 생성 → Litmus 적용.
set -euo pipefail

CLUSTER_NAME="${1:-codereferee}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if ! command -v kind >/dev/null 2>&1 && [ ! -x "$ROOT/.runtime/tools/kind" ]; then
  "$SCRIPT_DIR/install_kind.sh"
fi

# bootstrap_kind를 source해 CODEREFEREE_* 변수를 이 셸로 가져온다(litmus가 context를 쓴다).
# shellcheck source=/dev/null
. "$SCRIPT_DIR/bootstrap_kind.sh" "$CLUSTER_NAME"
"$SCRIPT_DIR/bootstrap_litmus.sh" "$CODEREFEREE_KUBECTL_CONTEXT"

echo "CodeReferee local Sandbox runtime is ready."
