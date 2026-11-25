"""앱 Pod 가 바깥으로 나갈 수 있는 상태인지. 리포트에 측정 조건으로 싣는다.

검사 대상 코드는 남의 서비스를 부를 수 있다. 우리는 거기에 부하까지 건다. 기본값이
10,50,100,200 명씩 20초라 한 번에 7천 건이 넘는다. 2026-11-25 실측에서 검사 대상이
카카오 검색 API 를 부르는 것을 보고 노드 방화벽으로 Pod 대역의 외부 연결을 끊었다.

끊는 것만으로는 부족하다. 외부를 쓰는 앱은 그 기능이 죽은 채로 돌고, 카오스 중에 그
경로가 500 을 내면 우리가 끊어서인지 앱이 약해서인지 리포트만 보고는 알 수 없다.
그래서 무엇을 끊고 측정했는지를 리포트에 남긴다.

상태를 지어내지 않고 실제 규칙을 읽는다. 규칙은 노드 컨테이너 안에 있어 노드가 다시
만들어지면 사라지므로, 'blocked' 를 상수로 박아 두면 사라진 날 리포트가 거짓말을 한다.
"""
from __future__ import annotations

import os
import subprocess

try:
    from .collect_baseline import kubectl_environment
except ImportError:
    from collect_baseline import kubectl_environment

CHAIN = 'CODEREFEREE-EGRESS'
POD_CIDR = '10.244.0.0/16'
BLOCKING_TARGETS = ('REJECT', 'DROP')
MAX_RULES = 16


def _node_names(environment: dict, timeout: int) -> list[str]:
    listed = subprocess.run([os.getenv('CODEREFEREE_KIND_COMMAND', 'kind'), 'get', 'nodes',
                             '--name', os.getenv('CODEREFEREE_KIND_CLUSTER_NAME', 'codereferee')],
                            capture_output=True, text=True, env=environment, timeout=timeout)
    if listed.returncode:
        raise RuntimeError(listed.stderr.strip()[:200] or 'kind get nodes failed')
    return [line.strip() for line in listed.stdout.splitlines() if line.strip()]


def _node_state(node: str, environment: dict, timeout: int) -> tuple[bool, list[str]]:
    """그 노드에서 Pod 대역의 외부 연결이 실제로 끊기는가, 그리고 그 근거 규칙."""
    attached = subprocess.run(['docker', 'exec', node, 'iptables', '-C', 'FORWARD',
                               '-s', POD_CIDR, '-j', CHAIN],
                              capture_output=True, text=True, env=environment, timeout=timeout)
    if attached.returncode:
        return False, []
    listed = subprocess.run(['docker', 'exec', node, 'iptables', '-S', CHAIN],
                            capture_output=True, text=True, env=environment, timeout=timeout)
    if listed.returncode:
        return False, []
    rules = [line.strip() for line in listed.stdout.splitlines() if line.strip()][:MAX_RULES]
    # 체인을 걸어 두기만 하고 비우면 전부 통과한다. 거부 규칙이 있는지까지 봐야 막힌 것이다.
    # 대상은 -j 바로 뒤다. 마지막 토큰을 보면 '--reject-with icmp-port-unreachable' 에 속는다.
    return any(_target(rule) in BLOCKING_TARGETS for rule in rules if rule.startswith('-A')), rules


def _target(rule: str) -> str:
    tokens = rule.split()
    return tokens[tokens.index('-j') + 1] if '-j' in tokens[:-1] else ''


def external_egress_state(*, timeout: int = 20) -> dict:
    """앱 Pod 의 외부 연결 상태. 실패해도 배포를 막지 않는다(측정 조건 기록용)."""
    if os.getenv('CODEREFEREE_CLUSTER_PROVIDER') != 'kind':
        return {'external_egress': 'unknown', 'reason': 'cluster_provider_not_kind'}
    environment = kubectl_environment()
    try:
        nodes = _node_names(environment, timeout)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return {'external_egress': 'unknown', 'reason': str(exc)[:200]}
    if not nodes:
        return {'external_egress': 'unknown', 'reason': 'no_cluster_nodes'}
    blocked, evidence = [], {}
    for node in nodes:
        try:
            node_blocked, rules = _node_state(node, environment, timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return {'external_egress': 'unknown', 'reason': str(exc)[:200]}
        blocked.append(node_blocked)
        evidence[node] = rules
    if not all(blocked):
        # 한 노드라도 열려 있으면 그 노드에 뜬 Pod 는 바깥으로 나간다.
        return {'external_egress': 'allowed', 'reason': 'node_firewall_absent',
                'nodes': [node for node, ok in zip(nodes, blocked) if not ok]}
    return {'external_egress': 'blocked', 'enforced_by': 'node_firewall',
            'scope': POD_CIDR, 'still_allowed': ['cluster_internal', 'dns'],
            'measurement_note': ('External services were unreachable during this run, so features '
                                 'that call them did not work. Failures on those paths are not '
                                 'evidence about the repository.'),
            'rules': evidence}
