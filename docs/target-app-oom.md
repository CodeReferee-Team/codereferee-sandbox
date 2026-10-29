# 앱 OOM 실험과 실행 제한

## 수정 이유

10월 23일 회의의 C안이다. 기존 `pod-memory-hog`는 앱 컨테이너 안의 부하 프로세스가
OOM으로 종료되고 앱은 살아남을 수 있었다. `oom_observed: false`라도 정상 HTTP만 보고
복구 성공으로 반환하던 경로를 제거한다. 커널이 항상 가장 큰 프로세스를 고르는 것은 아니며,
중요한 것은 **선택한 앱 자체가 OOM으로 종료됐는지**다.

## 현재 동작

- 외부 요청의 `chaosMode: litmus_pod_memory_oom`, custom alias `oom`은 유지한다.
  내부 구현은 Litmus 대신 `scripts/target_oom.py`를 사용한다.
- Deployment 소유의 Ready Pod를 고정하고, 지정한 컨테이너의 원래 containerd ID를 기록한다.
  `podsAffectedCount: 1`이면 정확히 1개, 생략하면 모든 대상 Pod(최대 16개)를 선택한다.
- 각 대상과 같은 노드에 제한된 수명의 privileged helper Pod를 띄운다. 앱 이미지에
  Python·stress-ng를 설치하거나 앱 코드를 바꾸지 않는다.
- helper는 **Pod UID와 컨테이너 ID가 모두 맞는 leaf cgroup 하나**만 찾는다.
  그 컨테이너의 `memory.max`를 잠시 1 byte로 낮춰 커널 OOM을 유발한다.
  노드·Pod 전체·DB·관측기의 메모리 한도는 변경하지 않는다.
- helper는 대상 cgroup 밖에서 실행되므로 앱 내부 stress 프로세스가 대신 죽는 문제가 없다.
  OOM counter 증가/기존 cgroup 소멸을 기다리고, 최대 15초 후 `finally`에서 원래 한도를 복원한다.
  이미 런타임이 기존 cgroup을 제거했다면 `target_cgroup_removed`를 기록한다.
  새 컨테이너는 Deployment에 선언된 원래 한도로 생성된다.
- helper 종료만으로는 성공이 아니다. 같은 Pod UID의 **원래 컨테이너 ID**에 대해
  이번 실험의 `OOMKilled`, 종료 코드 137이 확인돼야 한다. kubelet의 초 단위 종료 시각도 처리한다.
- 선택한 모든 대상의 OOM이 확인되고, 모두 다시 Ready이며 HTTP가 연속 5회 성공해야 복구다.
  서비스 응답만 살아 있는 다른 복제본을 대상의 복구로 오인하지 않는다.
- helper는 `activeDeadlineSeconds: 45`, 종료 유예 10초이며 부모 runner는 `finally`에서
  삭제한다. API의 요청 namespace 정리도 기존대로 수행한다.

이는 **일시적 메모리 한도 축소에 대한 OOM 복구 실험**이다. 앱의 자연적인 메모리 누수나
실제 운영 부하에서의 메모리 수요를 증명하는 실험은 아니다. `--fault-seconds`는 다른
Litmus 부하의 지속 시간이며 OOM은 대상별 1회 주입이다.

지원 조건: Linux Kubernetes 노드, containerd, cgroup v2, 읽기/쓰기가 가능한
`/sys/fs/cgroup`, helper 생성·삭제/로그 조회 권한 및 privileged Pod 허용.
앱의 명시적 memory limit은 32Mi..4Gi여야 한다. Windows PowerShell과 Bash/Mac 모두
같은 Python runner를 호출한다. Mac 호스트에서도 실제 주입 대상은 Linux 노드다.
지원 조건이 맞지 않거나 대상이 바뀌어 정확한 cgroup을 찾을 수 없으면 주입 오류로 반환한다.

커널 동작 근거: [Linux cgroup v2 memory.max 문서](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).

## 응답과 실패 분류

| 상황 | observationStatus | exitCode | timedOut | infraError |
|---|---|---|---|---|
| 실제 앱 OOM + 대상/HTTP 복구 | observed | 0 | false | null |
| 실제 앱 OOM + 복구 관측창 내 미복구 | observed | 1 | true | null |
| 앱 OOM 근거 없음 | infrastructure_error | null | false | oom_not_triggered |
| helper/권한/cgroup 접근 실패 | infrastructure_error | null | false | oom_injection_failed |
| 기존 한도 복원 실패 | infrastructure_error | null | false | oom_restore_failed |
| helper 삭제 실패 | infrastructure_error | null | false | oom_helper_cleanup_failed |
| 준비 명령/요청 전체의 실행 상한 초과 | infrastructure_error | null | true | sandbox_execution_timeout 또는 sandbox_budget_exceeded |

`exitCode: 0`은 Sandbox가 확인한 복구 결과이며 서비스 SLO의 최종 PASS는 AI가 정한다.
`oom_observed: false`는 OOM이 없었다는 단정이 아니라 **정확한 대상 OOM을 증명하지 못했다**는 뜻이다.

OOM 세부 근거는 `chaos_observation.target_oom_evidence`에 보존한다:
Pod 이름/UID, 컨테이너 이름/원래 ID, 전후 restartCount, 종료 사유·코드·시각.
`chaosResult.mechanism`과 `chaos_observation.kill_method`는 `cgroup_v2_memory_max`이고
`chaosEngine`은 null이다. Litmus verdict를 지어내지 않는다.
`chaosResult.helper_reports`에는 한도 원복 결과와 대상 cgroup의 OOM counter를 남긴다.
AI의 단일 결과 파서는 `chaosResult`를 보존하지 않으므로 같은 진단을
`chaos_observation.oom_injection`에도 포함한다. AI 코드·Judge 정책은 변경하지 않는다.

`failureKind`/`failurePhase`는 원인 종류와 injection/restoration/cleanup/recovery 단계를 구분하는
추가 필드다. 현재 AI 파서가 유지하는 `source.failure`에도 같은 정보를 보존한다.
묶음의 미관측 OOM은 `stopped`에 남고 `partial`이 된다. 관측한 다른 시나리오 결과가 있어도
**OOM까지 통과한 전체 검사**로 설명해서는 안 된다. 이 동작은 최신 dev의 부분 실행 정책을 유지한다.

## 타임아웃: 현재 구현과 회의 합의

최신 dev에 이미 요청 `deadlineSeconds`, 전체 예산에서 정리용 90초 예약, API 단계별 제한,
시간이 부족한 묶음 시나리오 건너뛰기, 부분 결과 보존이 반영돼 있다.
AI는 HTTP 대기 시간보다 30초 짧은 deadline을 보낸다. 기본 HTTP 600초라면 Sandbox 실행 예산은
570 - 90 = 480초다. 준비 배포 자체 상한은 1200초, smoke는 180초, 시나리오는 300초이며
요청 예산이 더 작으면 API가 그보다 먼저 멈춘다. 기한이 없는 호출은 단계별 상한을 사용한다.

이번에는 준비 명령의 `subprocess.TimeoutExpired`와 빌드/테스트 검사 timeout도
사용자 결함으로 반환하지 않도록 보완한다. `stoppedAt`/`failurePhase`에 실제 단계를,
`failureKind: execution_limit`, `validationSupport: unsupported_within_budget`를 남긴다.
`budget`의 elapsedSeconds는 요청 경과 시간, limitSeconds는 실제 적용 상한이며
`elapsedScope`/`limitScope`로 요청/단계 범위를 명시한다. AI가 파싱하는
`sandboxReport.execution_limit` 또는 `source.execution_limit`에도 보존한다.

이 결과는 **현재 자원·시간 제한 안에서는 검증할 수 없었다**는 뜻이다. 느린 레포의
코드 결함이나 영구적인 지원 불가로 단정하지 않는다. 실제 장애 주입과 신뢰할 수 있는 관측을
확보한 뒤의 복구 시간 초과는 위의 `observed + exitCode 1`과 구분한다.
프로세스 트리 종료는 POSIX에서는 프로세스 그룹, Windows에서는 `taskkill /T /F`를 사용한다.
일반적인 앱 CrashLoop나 readiness 오류의 근거가 있는 배포 실패 분류는 유지한다.

남은 사항: rollout 대기 실패에서 실제 부팅 결함과 단순한 느린 기동을 진단 근거에 따라
더 세밀하게 구분하기, 내부 명령까지 같은 deadline을 직접 전달하기, 준비 프로세스 강제 종료 시
아직 부모에게 전달되지 않은 namespace/이미지/build 작업의 확실한 회수, cleanup 예산의 강제 보장,
연결 종료 감지·취소 및 비동기 job API. 이번 수정으로 이 설계 전체가 완성된 것은 아니다.
실행 전 AI 소요 시간 예측이나 자동 거절은 추가하지 않는다.

## 로컬 재검증

기존 앱을 건드리지 않고 전용 fixture namespace를 생성·정리하는 opt-in 드라이버:

```bash
python scripts/verify_oom_scenario.py --run-live --case recovered
python scripts/verify_oom_scenario.py --run-live --case single-target
python scripts/verify_oom_scenario.py --run-live --case all-targets
python scripts/verify_oom_scenario.py --run-live --case unrecovered
python scripts/verify_oom_scenario.py --run-live --case memory-pressure
python scripts/verify_oom_scenario.py --run-live --case container-kill
```

PowerShell에서도 같은 명령을 사용한다. 실행 전에 `CODEREFEREE_KUBECTL_CONTEXT`를 원하는
테스트 클러스터로 지정한다. 원본 JSON은 Git 추적 밖 `.runtime/evidence/oom-*.json`에 기록한다.
이 드라이버는 baseline 5회, 부하 지속 5초, 복구 관측 120초(미복구 fixture는 45초)다.
API 기본 관측창 240초와 구분한다. 0초 복구는 HTTP 실패 표본이 없었다는 뜻이다.

### 2026-10-28 실측

Windows + Docker Desktop, kind-codereferee, Linux 노드 cgroup v2 / containerd 환경이다.

| 케이스 | 실제 대상 OOM | 복구 | Sandbox 결과 |
|---|---|---|---|
| 복제본 1개 | OOMKilled/137, 원래 컨테이너 ID 일치 | HTTP 복구 9.40초 | observed / 0 |
| 복제본 2개 중 1개 선택 | 선택한 1개만 재시작, 다른 Pod restartCount 0 | HTTP 실패 표본 없음 | observed / 0 |
| 복제본 2개 전체 | 두 대상 모두 원래 컨테이너 ID의 OOM 확인 | HTTP 복구 10.75초 | observed / 0 |
| OOM 후 HTTP 503을 계속 반환하는 fixture | OOM 확인 | 관측창 내 미복구 | observed / 1 / timedOut true |
| 기존 Memory Pressure | OOM 시나리오 아님 | 정상 복구 | observed / 0 |
| 기존 Container Kill | OOM 시나리오 아님 | HTTP 복구 7.39초 | observed / 0 |
| 실제 Sandbox HTTP POST로 OOM → Container Kill 묶음 | OOM 근거 포함 | 두 시나리오 복구 | observed / 0 / partial false |

첫 전체 2개 대상 검사는 짧게 설정한 45초 관측창에서 복구 확인을 마치지 못했다.
이때도 성공으로 덮지 않고 `observed / 1 / timedOut true`로 기록됐다.
관측창을 120초로 두고 다시 실행해 복구를 확인했다. API 기본 240초는 바꾸지 않았다.
주입 한도를 너무 빨리 원복하면 OOM을 놓치는 경우도 발견해, OOM counter/대상 cgroup 소멸을
확인한 뒤 원복하도록 보완했다. 관측 근거가 없을 때는 계속 판정 불가다.

드라이버는 원래 Deployment limit과 실행 중인 컨테이너의 실제 `memory.max`도 확인한다.
전용 namespace·helper·observer를 삭제했고 원래 fixture 및 노드는 유지했다.
AI의 현재 HTTP 응답 파서에 실측 JSON을 넣어 OOM 근거와 observed/0, observed/1 구분이
보존되는 것을 확인했다.

- Linux Python 3.12 컨테이너: 전체 173개 테스트 통과(POSIX Bash 테스트 포함).
- Windows Python: POSIX 셸 대역 테스트 12개를 분리한 161개 중 160개 통과,
  POSIX 파일 권한 테스트 1개 제외. 셸 대역 테스트는 Windows에서 PATH/실행 파일 해석 차이로
  통과하지 않아 Linux에서 따로 확인했다. 이를 Mac 실측으로 표현하지 않는다.
- 근거 없는 OOM 성공 차단, 다른 컨테이너·과거 종료·다른 Pod 제외, 원복 실패 차단,
  실제 OOM 후 미복구, 실행 timeout의 ERROR 분류, Windows/POSIX 하위 프로세스 종료를 확인했다.

검증 범위는 Sandbox 내부/HTTP API와 AI 파서다. Backend·Redis·프론트 전체 흐름,
LLM 자동 패치 생성, AWS·Mac 실측을 이번에 다시 수행한 것은 아니다.
