#!/usr/bin/env bash
# bootstrap_kind.ps1의 POSIX 포팅. kind 클러스터를 만들고 CODEREFEREE_* 환경변수를 설정한다.
# 변수를 현재 셸에 남기려면 dot-source 하라:  . ./scripts/bootstrap_kind.sh
#
# source 로 실행되는 스크립트라 두 가지를 지킨다.
#   1. 실패해도 exit 하지 않는다. sourced 상태의 exit 는 호출한 셸을 그대로 종료시켜서,
#      user-data 는 뒤 단계가 통째로 실행되지 않고 SSM 세션은 메시지 없이 끊긴다.
#   2. set 옵션을 호출한 셸에 남기지 않는다. -u 가 남으면 그 뒤에 같은 셸에서 띄우는
#      uvicorn 이 미정의 변수 참조 한 번에 죽는다.
# 본문은 함수 안에 두고, 실패는 errexit 에 기대지 않고 명시적으로 검사한다.
# (조건식에서 호출된 함수 안에서는 errexit 가 적용되지 않는다.)

# 검사 대상 Pod 가 바깥으로 나가지 못하게 막는다.
#
# 검사 대상은 남의 서비스를 부를 수 있고 우리는 거기에 부하까지 건다. 부하 기본값이
# 10,50,100,200 명씩 20초라 한 번에 7천 건이 넘는다. 2026-11-25 실측에서 검사 대상이
# 카카오 검색 API 를 부르는 것을 확인했다. 남의 서버를 두드리는 것은 우리가 할 일이 아니다.
# VPC 도 함께 막힌다. 안 막으면 검사 대상 코드가 BE 의 인증 없는 API 와 Redis 에 닿는다.
#
# 노드 안쪽 FORWARD 에 건다. 호스트의 DOCKER-USER 는 늦다. kind-masq-agent 가 POSTROUTING
# 에서 MASQUERADE 하므로 호스트에 도착할 때는 출발지가 이미 노드 IP 라 Pod 를 구분할 수 없다.
# NetworkPolicy 도 쓸 수 없다. 기본 CNI 인 kindnetd 는 그것을 조용히 무시한다.
#
# DROP 이 아니라 REJECT 다. DROP 이면 앱이 연결 타임아웃까지 매달려서 기동 프로브가 깨지고,
# '외부 차단' 이 '앱이 안 뜸' 으로 보고된다. REJECT 는 8ms 만에 거절한다(실측).
# DNS 는 통과시킨다. 이름은 풀리고 연결만 막히는 것이 앱에게 가장 빠른 실패다.
#
# 노드는 막지 않는다. 의존성 이미지(postgres:17-alpine 등)를 런타임에 받아오기 때문이다.
_codereferee_block_pod_egress() {
  local kind="$1" cluster_name="$2" node chain="CODEREFEREE-EGRESS" pods="10.244.0.0/16"
  for node in $("$kind" get nodes --name "$cluster_name" 2>/dev/null); do
    docker exec "$node" iptables -N "$chain" 2>/dev/null \
      || docker exec "$node" iptables -F "$chain" 2>/dev/null || continue
    docker exec "$node" iptables -A "$chain" -d 10.0.0.0/8 -j RETURN
    docker exec "$node" iptables -A "$chain" -p udp --dport 53 -j RETURN
    docker exec "$node" iptables -A "$chain" -p tcp --dport 53 -j RETURN
    docker exec "$node" iptables -A "$chain" -j REJECT --reject-with icmp-port-unreachable
    docker exec "$node" iptables -C FORWARD -s "$pods" -j "$chain" 2>/dev/null \
      || docker exec "$node" iptables -I FORWARD 1 -s "$pods" -j "$chain"
  done
}

_codereferee_bootstrap_kind() {
  local cluster_name="${1:-codereferee}" root kind context
  [[ "$cluster_name" =~ ^[a-z0-9][a-z0-9.-]*$ ]] || {
    echo "ClusterName must contain only lower-case letters, numbers, dots, and hyphens." >&2; return 1; }

  for cmd in docker kubectl; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "$cmd was not found in PATH." >&2; return 1; }
  done

  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || return 1
  if command -v kind >/dev/null 2>&1; then
    kind="$(command -v kind)"
  elif [ -x "$root/.runtime/tools/kind" ]; then
    kind="$root/.runtime/tools/kind"
  else
    echo "kind was not found. Run ./scripts/install_kind.sh first." >&2; return 1
  fi

  docker info >/dev/null || { echo "docker is not usable. Is the daemon running and is this user in the docker group?" >&2; return 1; }
  if ! "$kind" get clusters 2>/dev/null | grep -qx "$cluster_name"; then
    "$kind" create cluster --name "$cluster_name" --wait 180s || { echo "kind failed to create cluster $cluster_name." >&2; return 1; }
  fi

  context="kind-$cluster_name"
  kubectl --context "$context" get nodes || { echo "kubectl cannot reach context $context." >&2; return 1; }

  _codereferee_block_pod_egress "$kind" "$cluster_name"

  export CODEREFEREE_CLUSTER_PROVIDER="kind"
  export CODEREFEREE_KIND_CLUSTER_NAME="$cluster_name"
  export CODEREFEREE_KUBECTL_CONTEXT="$context"
  export CODEREFEREE_KIND_COMMAND="$kind"

  echo "kind cluster is ready: $cluster_name"
  echo "Configured: CODEREFEREE_KUBECTL_CONTEXT=$context (dot-source to keep in your shell)"
}

# 변수명에 스크립트 이름을 붙인다. bootstrap_local.sh 가 이 파일을 dot-source 하므로
# 같은 이름을 쓰면 안쪽에서 unset 할 때 바깥쪽 복원이 조용히 깨진다.
_codereferee_opts_kind="$(set +o)"
# Command substitution clears errexit unless inherit_errexit is enabled.
# Restore the caller's actual flag, not the subshell's altered snapshot.
case $- in *e*) _codereferee_opts_kind="$_codereferee_opts_kind; set -e" ;; esac
set -uo pipefail
if _codereferee_bootstrap_kind "$@"; then _codereferee_rc_kind=0; else _codereferee_rc_kind=$?; fi
eval "$_codereferee_opts_kind"
unset _codereferee_opts_kind
unset -f _codereferee_bootstrap_kind
unset -f _codereferee_block_pod_egress

# eval 로 값을 먼저 펼쳐서, 호출한 셸에 임시 변수도 남기지 않는다.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  eval "unset _codereferee_rc_kind; return $_codereferee_rc_kind"
else
  exit "$_codereferee_rc_kind"
fi
