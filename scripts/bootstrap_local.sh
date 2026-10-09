#!/usr/bin/env bash
# bootstrap_local.ps1의 POSIX 포팅.
# kind/kubectl 설치(필요 시) -> 클러스터 생성 -> Litmus 적용 -> 실행할 파이썬 확정.
#
# source 로 실행한다. 그래야 CODEREFEREE_* 변수가 셸에 남아 같은 셸에서 API 를 띄울 수 있다.
#   source scripts/bootstrap_local.sh codereferee
#   "$CODEREFEREE_PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port 8100
# 실패해도 호출한 셸을 종료시키지 않고 set 옵션도 남기지 않는다. 이유는 bootstrap_kind.sh 주석 참조.

_codereferee_bootstrap_local() {
  local cluster_name="${1:-codereferee}" script_dir root candidate preferred
  # 운영자가 지정한 인터프리터를 먼저 본다. 아래에서 변수를 비우기 전에 떠둬야 한다.
  preferred="${CODEREFEREE_PYTHON:-}"

  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || return 1
  root="$(cd "$script_dir/.." && pwd)" || return 1

  if ! command -v kind >/dev/null 2>&1 && [ ! -x "$root/.runtime/tools/kind" ]; then
    "$script_dir/install_kind.sh" || { echo "kind installation failed." >&2; return 1; }
  fi
  # Amazon Linux 2023 을 비롯한 새 호스트에는 kubectl 이 없다. kind 와 같은 방식으로 받는다.
  if ! command -v kubectl >/dev/null 2>&1 && [ ! -x "$root/.runtime/tools/kubectl" ]; then
    "$script_dir/install_kubectl.sh" || { echo "kubectl installation failed." >&2; return 1; }
  fi
  # 받은 도구는 PATH 에 올려야 bootstrap_kind.sh 와 이후 명령이 찾는다.
  case ":$PATH:" in
    *":$root/.runtime/tools:"*) ;;
    *) PATH="$root/.runtime/tools:$PATH"; export PATH ;;
  esac

  # shellcheck source=/dev/null
  . "$script_dir/bootstrap_kind.sh" "$cluster_name" || return 1
  "$script_dir/bootstrap_litmus.sh" "$CODEREFEREE_KUBECTL_CONTEXT" || return 1

  # app/main.py 와 app/scenarios.py 가 'X | None' (PEP 604) 를 쓴다. 3.9 에서는 import 자체가
  # TypeError 로 깨지는데, Amazon Linux 2023 의 기본 python3 이 3.9 라 새 호스트에서 바로 걸린다.
  # 여기서 먼저 걸러서, uvicorn 을 띄우고 나서야 알게 되는 일이 없게 한다.
  CODEREFEREE_PYTHON=""
  for candidate in "$preferred" python3.13 python3.12 python3.11 python3.10 python3 python; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      CODEREFEREE_PYTHON="$(command -v "$candidate")"
      break
    fi
  done
  if [ -z "$CODEREFEREE_PYTHON" ]; then
    # 실패했으면 빈 변수를 호출한 셸에 남기지 않는다.
    unset CODEREFEREE_PYTHON
    echo "Python 3.10 or newer was not found. The Sandbox API uses PEP 604 unions and will not import on 3.9." >&2
    echo "On Amazon Linux 2023: sudo dnf install -y python3.11 python3.11-pip" >&2
    return 1
  fi
  export CODEREFEREE_PYTHON

  echo "CodeReferee local Sandbox runtime is ready."
  echo "Configured: CODEREFEREE_PYTHON=$CODEREFEREE_PYTHON"
}

_codereferee_opts_local="$(set +o)"
set -uo pipefail
if _codereferee_bootstrap_local "$@"; then _codereferee_rc_local=0; else _codereferee_rc_local=$?; fi
eval "$_codereferee_opts_local"
unset _codereferee_opts_local
unset -f _codereferee_bootstrap_local

if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  eval "unset _codereferee_rc_local; return $_codereferee_rc_local"
else
  exit "$_codereferee_rc_local"
fi
