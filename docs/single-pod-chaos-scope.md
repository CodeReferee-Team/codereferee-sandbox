# Litmus 장애 범위: 전체 Pod와 정확한 개수

기존 Litmus 시나리오는 `PODS_AFFECTED_PERC=100`으로 전체 대상을 공격한다.
이는 전체 소실/상관 장애의 복구 실험이다. replica 증설의 효과를 확인하는
독립 단일 장애 실험과 구분해야 한다. 기본값과 기존 시나리오 ID는 변경하지 않는다.

## 요청 계약

```json
{
  "repositoryUrl": "https://github.com/CodeReferee-Team/codereferee-chaos-demo.git",
  "branch": "master",
  "requestId": "single-pod-example",
  "chaosMode": "litmus_container_kill",
  "podsAffectedCount": 1
}
```

- 선택 필드 `podsAffectedCount` / `pods_affected_count`: 엄격한 정수 1..16.
- 생략/null: 기존 전체 대상(`PODS_AFFECTED_PERC=100`).
- 설정: namespace와 Deployment→ReplicaSet→Pod controller UID 소유권을 확인하고,
  Running/Ready·대상 컨테이너 Ready·삭제 진행 아님을 만족하는 정확히 N개를 선택한다.
- 부족하면 전체 대상으로 확대하거나 비율을 반올림하지 않고 주입 전 중단한다.
- 사용자가 Pod 이름을 직접 전달하지 않는다. 이름은 각 배포 후 Sandbox가 선택한다.
- 일반/fixture/API 기반 단독 시나리오에서 설정하면 HTTP 422이다.
- 혼합 검사 묶음에서는 **Litmus 단계에만 적용**된다. Scale Down, 라우팅 단절,
  의존성 장애는 여전히 해당 시나리오 고유 범위로 실행된다.
- 최초 실행과 patch 재검증에 동일 count 정책을 전달해야 한다. 재배포된 Pod 이름은
  달라도 된다. AI 요청 배선·Judge 정책·Frontend 선택 UI는 이 변경에 포함하지 않는다.

## Litmus 매핑과 안전성

고정 go-runner 3.30.0은 명시한 `TARGET_PODS`가 있을 때 비율 기반 선택을 우회한다.
따라서 `PODS_AFFECTED_PERC=100`은 유지하되 선택한 이름을 `TARGET_PODS`로 전달한다.
설치된 6종 fault 정의는 이미 이 필드를 지원하므로 bootstrap 변경은 필요 없다.
OOM도 pod-memory-hog를 재사용한다. 컨테이너 런타임/버전은 기존 고정값을 유지한다.

공식 고정 소스:
[GetTargetPods/GetPodList](https://github.com/litmuschaos/litmus-go/blob/72a9ff82a2b46c25d6fefc8d397cb91f8c03b38c/pkg/utils/common/pods.go).
선택된 이름이 사라지면 Litmus가 대상 선택 오류로 실패하며 전체 공격으로 확대하지 않는다.
이름 지정은 UID 잠금이 아니므로 주입 전 기록과 실제 결과를 함께 검토한다.

## 관측 근거

`chaos_observation.fault_scope`:

- `mode`, `requested_count`, `selection_method`
- `selected_pods`: 선택한 이름/UID/node/container ID/재시작 횟수
- `pods_before`, `pods_after`: 대상 Deployment 소유 Pod들의 전후 상태
- `litmus_reported_targets`: Litmus 원본 history
- `unexpected_reported_pods`, `missing_reported_pods`, `reported_pod_scope_matches`

컨테이너 재시작은 동일 UID와 재시작 횟수로 확인한다. Pod 삭제는 새 UID와 비교한다.
여러 Pod 교체가 일어나면 전후 snapshot이 원본이며 단일 대표 UID만으로 1:1 교체를
증명하지 않는다. Container Kill history의 `targeted`도 정상 실행 기록이다.
Pod Delete는 history에 상위 Deployment를 기록할 수 있어 개별 Pod history가 없을 때
`reported_pod_scope_matches=null`이다. null/부분 history를 정확한 개수 성공 증명으로
취급하지 않는다. 예상 밖 Pod가 history에 있으면 인프라/실험 범위 오류로 처리한다.

`replicas=2`와 단일 Pod 장애가 반드시 Judge PASS를 뜻하지 않는다. 살아남은 Pod,
Service 라우팅, 실제 HTTP 표본 및 팀 합의 정책으로 판정해야 한다. replica 1개 중단도
요구된 SLO에 따라 해석한다. Sandbox는 통과를 보장하거나 Judge 기준을 변경하지 않는다.

## 로컬 검증

```bash
python scripts/verify_repository_pipeline.py \
  --repository-url https://github.com/CodeReferee-Team/codereferee-chaos-demo.git \
  --branch master --request-id single-pod-check \
  --chaos-mode litmus_container_kill --pods-affected-count 1 \
  --output .runtime/evidence/single-pod-check.json
```

최소 비교: replica1/count1 → replica2/count1 → replica2/옵션 생략(전체).
같은 commit과 같은 장애 파라미터로 비교하고, 마지막 케이스가 전체 대상임을 확인한다.
기존 외부 target 경로는 Deployment·ReplicaSet·Pod 조회 권한이 필요하다.

## AI 팀 연동 사항

AI가 Sandbox HTTP body에 `podsAffectedCount: 1`을 넣고, patch 재검증에서도 동일 값을
유지하면 된다. 사용자가 생성한 Pod 이름이나 UID를 AI에서 추측해 전송하지 않는다.
기존 `chaosMode`/검사 묶음 ID는 변경하지 않았다. 이 옵션은 서비스 replica 설정을
수정하지 않으며 replica 증설은 기존 `patchDiff` 또는 레포 설정 경로를 사용한다.
AI 모델·프롬프트·Judge 정책·Server 큐 계약은 이 PR에서 수정하지 않는다.
AI 측 배선 후 전체 Worker/Redis/Backend 시연 재검증은 별도 수행해야 한다.

## 플랫폼 검증 범위

API·runner·대상 선택은 Python이며 PowerShell 전용 구현이 아니다. Python CLI는
Bash/macOS에서도 같은 인자를 받는다. Windows에서 LF patch를 임시 파일에 저장할 때
CRLF로 변환하던 문제를 함께 보완했고, 실제 API patch 적용으로 확인한다.
Python 회귀 105개는 Windows에서, 기존 Bash bootstrap 회귀 4개는 WSL Linux
Python/Bash에서 분리 실행했다. Windows Python의 text subprocess와 WSL bash 조합은
경로·CRLF 차이로 기존 POSIX 테스트가 실패하므로 이 실패를 기능 통과로 세지 않았다.
Mac의 새 단일 Pod 옵션에 대한 실제 Chaos 실행은 이번 검증에 포함하지 않는다.
Bootstrap의 기존 Mac 검증 기록은 유지하며, Mac 실측을 Windows/WSL로 대체하지 않는다.

## 실제 API 검증 결과 — 2026-10-09

Windows + Docker Desktop의 기존 `kind-codereferee`에서 별도 localhost Sandbox API를
실행했다. 대상은 위 데모 레포의 고정 commit
`44679438bf1e835bc6ffa8eeb2ff39303f712ef4`이다. 세 요청 모두 실제 원격 clone,
build/test 경로, 이미지 build/load, 요청 namespace 배포, Litmus Container Kill,
Service를 통과하는 in-cluster HTTP 관측 및 정리를 수행했다.
replica2 요청은 실제 API의 `patchDiff`로 `.codereferee/validation.yaml`을 1→2로
수정했으며, 사람이 준비한 diff다. LLM patch 생성이나 AI Judge E2E를 검증했다고
주장하지 않는다. 메트릭 remote_write는 이번 범위 선택 실측에서 비활성화했다.

| 요청 조건 | 실제 재시작 Pod | HTTP 실패/전체 | 관측 복구(s) | Litmus |
| --- | --- | --- | --- | --- |
| replica1 / count1 | 1개, 0→1 | 11/57 | 47.69 | Pass |
| replica2 / count1 | 선택 1개만 0→1, 다른 Pod는 0 유지 | 1/57 | 4.09 | Pass |
| replica2 / 옵션 생략 | 2개 모두 0→1 | 13/62 | 41.98 | Pass |

세 결과는 모두 `observed`, `exitCode=0`, `recovered=true`다. 이는 실험과 HTTP 복구가
관측됐다는 뜻이고 세 서비스 모두 SRE PASS라는 뜻이 아니다. 두 count1 요청의
선택 이름·UID와 Litmus의 Pod `targeted` history가 일치했다. 옵션 생략 요청은
`TARGET_PODS`가 없고 `PODS_AFFECTED_PERC=100` 및 두 대상의 history를 확인했다.
세 요청 모두 namespace 제거, host/kind 요청 이미지 제거, clone 작업 공간 제거를 확인했다.

replica2/count1에서는 컨테이너 종료 직후 `Connection refused` 1건이 관측됐다.
0% 오류·무중단을 주장하지 않는다. 기본 readiness는 period2s/failureThreshold3이며
Service 대상 갱신 시점의 영향일 수 있으나 원인을 확정하지 않았다. 관측 복구는
표본의 첫 실패~마지막 실패 후 첫 성공 구간으로, 연속적 전체 다운타임 보장은 아니다.
이 raw evidence를 기준으로 AI 팀이 정책·최종 개선 판정을 재검증해야 한다.
결과를 PASS로 보이게 하려고 probe 실패를 재시도로 숨기거나 Judge 임계값을 바꾸지 않았다.

원본은 ignored `.runtime/evidence/single-pod-scope/`에 보존했다. 최초 kind 경로 누락의
인프라 실패와 Windows diff CRLF 적용 실패도 별도 파일로 남겼고 성공에 포함하지 않았다.
CRLF 실패는 patch 파일 저장의 플랫폼 줄바꿈 변환을 막은 뒤 실제 patch 적용으로 재검증했다.
초기 실측 드라이버의 replica2에서 실패 0건을 요구한 가정도 만족하지 않았으므로,
기능의 정확한 대상 범위 검증과 SRE 합격 검증을 구분했다. 샘플에 가짜 학습 라벨을
추가하지 않았다. CPU/네트워크/메모리 및 count>1의 새 옵션은 소스 호환·단위 테스트
범위이며, 이번 새 옵션의 live 검증은 Container Kill count1과 기존 전체 모드다.
