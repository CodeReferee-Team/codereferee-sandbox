# Runtime Chaos 시나리오와 검사 묶음

## 실행 범위

kind + Litmus에서 사용자 레포를 한 번 clone/build/배포한 뒤 선택한 시나리오를
순서대로 실행한다. 각 시나리오는 건강한 baseline을 확인한 후 주입하고, 해제 후
복구를 확인한다. 요청 namespace는 마지막에 정리한다.

### 묶음이 중간에 막혔을 때 (2026-10-23 개정)

- **대상이 복구하지 못하면** 거기서 멈추고 남은 시나리오는 `not_executed`에 적는다.
  복구하지 못한 대상에서는 다음 시나리오의 baseline부터 실패하기 때문이다.
- **인프라 오류(netem 없음, Litmus helper 실패, 시나리오 300초 초과)는 그 시나리오만
  `stopped`에 적고 다음 시나리오로 넘어간다.** 한 시나리오를 못 돌린 것은 다음 시나리오에
  대해 아무것도 말해주지 않는다. 예전에는 여기서 묶음 전체가 끝나 ERROR가 됐다(M10).
- 하나라도 관측했으면 묶음은 `observed`다. 판정은 관측한 시나리오로만 하고
  `chaos_observation.partial: true`, `executed`, `stopped`(시나리오, `infraError`,
  마지막 오류 줄), `not_executed`로 무엇이 빠졌는지 남긴다. AI는 이 목록을 리포트 본문에
  옮겨 빠진 검사를 통과한 것처럼 읽히지 않게 한다.
- 하나도 관측하지 못했으면 지금처럼 `infrastructure_error`이고 `stoppedAt`에 처음 막힌
  시나리오를 적는다.
- `recovery_seconds`는 가장 긴 복구(복구 상한 비교용), `total_downtime_seconds`는 복구 시간의
  합(에러 버짓용)이다. 에러 버짓은 장애마다 쓴 시간이 쌓인다.
- 요청 전체가 인프라 오류로 끝나면 `stoppedAt`이 `deploy`, `smoke`, `scenario:<chaosMode>` 중
  어디서 멈췄는지 알려준다.
- 단계 시간 초과는 프로세스 그룹 전체를 죽인다. 예전에는 직계 자식만 죽어서 그 아래
  `docker build`와 `kubectl`이 남았고, 출력 파이프를 쥔 채로 핸들러를 붙잡았다.
- 클러스터에 `ChaosExperiment` 정의가 없으면(`NotFound`) `bootstrap_litmus.sh`가 받아둔
  `.runtime/litmus/<실험>-v*.yaml`로 다시 설치하고 이어서 돈다. 부트스트랩이 operator rollout에서
  멈추면 정의가 하나도 깔리지 않아 묶음 전체가 인프라 오류가 됐다(2026-10-23 kind 실측).
  차트 파일도 없거나 `NotFound`가 아닌 kubectl 오류면 그대로 인프라 오류다.

### 요청 기한 `deadlineSeconds` (2026-10-23)

호출하는 쪽이 응답을 기다릴 수 있는 시간(초, 60~7200)을 요청에 넣으면 샌드박스가 그 안에서 끝낸다.
AI는 응답을 600초까지 기다리는데 샌드박스는 배포에만 1,200초를 쓸 수 있었다. AI가 먼저 포기하면
판정 불가가 나고, 샌드박스는 그 뒤에도 돌면서 락을 쥐어 다음 요청이 409를 받았다.

- 정리(namespace·이미지 삭제) 몫으로 90초를 빼고 남은 시간을 모든 단계가 나눠 쓴다.
  단계 상한(배포 20분, smoke 3분, 시나리오 5분)과 남은 시간 중 작은 쪽이 그 단계의 상한이다.
- 묶음에서는 남은 시간이 그 시나리오의 실측 소요(없으면 시나리오 상한 300초)보다 적으면 시작하지
  않는다. `stopped`에 `infraError: sandbox_budget_exceeded`와 남은 시간·필요 시간을 적고, 돈 시나리오로
  판정한다(부분 실행 규칙과 같다).
- 기한 때문에 끊긴 단계는 `sandbox_budget_exceeded`, 단계 상한에 걸린 것은 지금처럼
  `sandbox_execution_timeout`이다. 어느 쪽이 원인인지 갈라서 본다.
- 기한이 없으면 지금과 똑같이 단계별 상한만 쓴다.

새로 지원하는 `chaosMode`:

| 모드 | 주입 | 관측 |
|---|---|---|
| `litmus_container_kill` | 컨테이너 종료 | 같은 Pod UID, restart 증가, HTTP 복구 |
| `litmus_pod_cpu_hog` | 1 CPU worker, 80% load, 15초 | HTTP 응답과 fault revert |
| `litmus_pod_network_latency` | eth0 지연 300ms, 15초 | cluster 내부 HTTP 지연 |
| `litmus_pod_network_loss` | eth0 packet loss 50%, 15초 | 실제 요청 실패 또는 지연 |
| `litmus_pod_memory_hog` | 컨테이너 memory limit의 20%, 15초 | HTTP 및 종료 상태 |
| `litmus_pod_memory_oom` | 선택한 앱 컨테이너 cgroup의 한도를 일시 축소 후 원복 | 정확한 앱 OOMKilled + HTTP/대상 복구 |
| `dependency_database_outage` | 선언된 DB Pod의 packet loss 100%, 15초 | 프로필의 업무 API |
| `dependency_redis_outage` | 선언된 Redis Pod의 packet loss 100%, 15초 | 프로필의 업무 workflow |

메모리 실험은 컨테이너 limit이 32Mi..4Gi로 명시된 경우만 허용한다.
OOM은 더 이상 Litmus `pod-memory-hog`를 재사용하지 않는다. 기존 모드명은 호환성을 위해 유지한다.
대상 Pod UID·원래 컨테이너 ID·이번 실험의 `OOMKilled`/137 근거가 모두 맞아야
`oom_observed: true`다. 근거가 없으면 `oom_not_triggered`, `infrastructure_error`,
`exitCode: null`로 반환하며 성공으로 보지 않는다. 지원 조건과 실측 방법은
[앱 OOM 실험·실행 제한 계약](target-app-oom.md)을 참고한다.

## 관측 경로

관측 Pod에서 `Service DNS -> target Pod`로 HTTP 요청한다.
로컬에서는 관측 Pod만 port-forward한다. 대상 Pod를 직접 port-forward하는 방식과 달리
실제 target eth0의 netem 장애를 통과한다. 관측 Pod는 별도 label을 사용해 fault 대상에서 제외한다.

`probes.baseline`과 `probes.experiment`에 실제 요청 시각, HTTP 코드, 성공 여부,
지연, 오류를 보존한다. 관측 시스템 자체의 연결 실패는 사용자 요청 실패로 계산하지 않는다.
일시적인 관측 연결 오류는 최대 3회 시도하고 `observer_transport_errors`에 별도 보존한다.
3회 모두 실패하면 인프라 오류로 처리한다. 이때 잃어버린 요청의 결과를 추정하지 않는다.

`recovery_seconds`는 첫 실패 HTTP 요청부터 마지막 실패 이후 첫 성공 요청까지의
샘플 기반 시간이다. 이후 연속 성공 요청 5회로 복구를 검증한다. HTTP 실패가 관측되지
않으면 0이며, 성능 저하가 없었다는 뜻은 아니다. Litmus 스케줄링·이미지 pull 시간을
포함하는 `experiment_duration_seconds`와 구분한다. 원본 probe로 재계산할 수 있다.

CPU/메모리 사용량을 별도로 측정하지 않은 경우 해당 metric은 null이다.
fault 입력 강도를 실제 CPU/메모리 사용량으로 대신하지 않는다.

## 검사 등급

현재 기본 묶음은 조정 가능한 제품 설정이다. Google SRE나 업계 표준 등급이 아니며
Judge의 합격 기준을 바꾸지 않는다. 설정은 `app/scenarios.py`에 모아 둔다.

- `suite_quick`: Container Kill
- `suite_standard`: Container Kill, CPU Stress, Network Latency
- `suite_deep`: 위 3개 + Pod Delete, Packet Loss, Service Routing Blackhole,
  Deployment Scale Down, Rollout Restart
- DB/Redis 의존성과 메모리 실험은 추가 선택 가능

### 기본 구성을 정한 이유

현재 등급은 실행 범위·부담에 따른 초기 제품 기본값이며 소요 시간 보장이나 공인 등급이 아니다.
빠른 검사는 같은 Pod 안의 프로세스 재시작·HTTP 복구라는 좁은 장애 범위에 집중한다.
Container Kill이 모든 서비스에 보편적으로 필수라는 의미는 아니다. 컨테이너 웹 서비스의
기본 복구 검사로 선택했으며 배치 작업 등 다른 서비스 유형에는 재검토가 필요하다.
clone/build 및 애플리케이션 기동 때문에 빠른 검사도 수 분 걸릴 수 있다.

중간 검사는 복구와 CPU·네트워크 성능 저하를 함께 보고, 정밀 검사는 Pod 교체,
라우팅, 복제본과 배포 lifecycle까지 포함한다. DB/Redis에는 의존성·업무 API 선언이,
OOM에는 메모리 limit이 필요하므로 추가 선택으로 둔다.
공통 필수 절차는 정상 baseline 확인, 장애 해제·복구 관측, evidence 보존과 환경 정리다.
합격 기준은 서비스 SLO와 AI 팀 정책에 따라 정하며, 실제 소요 시간·관측 결과로 기본 구성을 조정한다.

각 시나리오 evidence는 `chaos_observation.scenarios`에 독립적으로 보존되어 기존
AI parser와 Backend report를 통해 전달된다. 합계 availability/error rate는 측정
요청 수로 계산한다. 개별 p95의 최대값을 전체 p95로 보고하지 않는다.
묶음 p95는 null이며 각 실험의 p95를 확인해야 한다.

현재 AI Judge는 전체 묶음의 복구 결과를 받을 수 있지만, 시나리오별 판정 정책은
AI 팀과 합의할 사항이다. Sandbox에서 AI 정책을 수정하지 않는다.

## 추가 선택 계약

Backend와 AI의 요청 필드는 변경하지 않고 기존 `chaos_mode` 문자열을 사용한다.
카탈로그: `GET /chaos/scenarios`

사용자 선택 예: `suite_custom__ck__cpu__lat`

| alias | 시나리오 |
|---|---|
| ck | Container Kill |
| pd | Pod Delete |
| cpu | CPU Stress |
| lat | Network Latency |
| loss | Packet Loss |
| route | Service Routing Blackhole |
| scale | Scale Down |
| roll | Rollout Restart |
| db | DB Dependency |
| redis | Redis Dependency |
| mem | Memory Pressure |
| oom | Memory Limit Exceeded |

64자 Backend 계약을 넘는 선택은 버전이 있는 compact selection
`suite_custom__v1_<hex>`로 전송한다. v1 bit 순서는 위 표 순서로 고정된다.
예: `suite_custom__v1_fff`는 12개 모두 선택. 중복은 제거하며 compact 모드는 표의 고정 순서로 실행한다.

## 요청 이미지 정리

namespace 삭제 완료 후 요청 전용 Docker 이미지와 kind node 이미지를 정리한다.
이미지는 request ID label을 포함하며, 정리는 namespace와 일치하는 정확한 태그만 허용한다.
결과의 `source.artifact_cleanup`에 삭제 여부와 오류를 기록한다. 전체 image prune이나
강제 삭제는 하지 않는다. Docker build cache와 공유 layer는 별도 정책의 대상이다.
프로세스 강제 종료로 finally가 실행되지 못한 경우의 label 기반 TTL 회수는 후속 과제다.

## 의존성 업무 probe

프로필 target에 `dependencies.database` 또는 `dependencies.redis`를 선언한다.
기본 QuickByte 프로필은 DB 식당 조회와 Redis 결제 승인·큐 발행을 사용한다.
Redis probe의 setup/steps는 선언형 HTTP workflow이며 다른 레포는 해당 레포의 API로
구성하면 된다. QuickByte API 이름은 generic runner에 하드코딩하지 않는다.
setup에서 사용하는 비밀번호는 이번 Sandbox 배포가 생성한 임시 값이다.
비밀번호·토큰·응답 body는 evidence에 포함하지 않는다.

## 로컬 전체 연동

네 레포를 같은 부모 디렉터리에 두고 Docker/kind bootstrap 및 각 레포 의존성 설치 후:

```powershell
. ./scripts/start_local_integration.ps1 -DisableLlm
.venv/Scripts/python.exe scripts/verify_backend_pipeline.py `
  --mode suite_custom__db__redis `
  --verify-postgres `
  --output .runtime/evidence/full-pipeline.json
```

Frontend 5173, Backend 18090, Sandbox 8100을 사용한다.
Frontend 프록시는 `CODEREFEREE_BACKEND_URL`로 변경 가능하다.
Windows Docker의 예약 포트 때문에 8080이 bind되지 않는 환경을 고려했다.
`-DisableLlm`은 실제 모델 호출 없이 규칙 판정과 fallback 보고서로 전송·실행 경로를
검증한다. 실제 LLM의 설명·패치 생성 품질 검증은 별도다.

## 동시 사용자 부하 (`load_concurrent_users`, 2026-10-30)

장애 시나리오는 0.5~1초에 한 번 보내는 확인 요청만으로 관측한다. 그래서 "사용자가 늘어도 버티는가"에는
답하지 못했다. 이 시나리오는 장애를 넣지 않고 동시 사용자를 단계적으로 늘린다.

- 요청 네임스페이스 안에 부하 Pod(`python:3.12-alpine`, CPU 1코어·메모리 256Mi 상한)를 띄워 Service로 직접
  보낸다. port-forward를 거치지 않는다.
- 기본 단계는 10, 50, 100, 200명이고 단계마다 20초다. 사용자 한 명은 요청을 보내고 응답을 받은 뒤, 직전
  요청 시작부터 1초가 지나야 다음 요청을 보낸다. 그래서 N명이면 서비스가 빠를 때 초당 약 N건이다.
- 응답이 5초 안에 없거나, 5xx·429면 실패다. 다른 4xx(인증 필요, 없는 경로)는 서비스가 살아서 답한 것으로 본다.
- 단계마다 요청 수, 실패 수, 처리량, p50·p95·p99 응답 시간, 상태 코드별 건수를 낸다. 요약으로
  `max_users_without_errors`(첫 실패 단계 직전의 사용자 수)와 `first_users_with_errors`를 낸다.
  응답 시간에는 임계값을 두지 않는다. 몇 ms부터 느린지는 서비스마다 다르다.
- `latency_growth`: 첫 단계와 응답을 받은 마지막 단계의 p95를 나란히 두고 배율을 낸다. "사용자가 늘면 얼마나
  느려지나"의 답이다. 이것도 사실로만 내고 판정하지 않는다.
- `overload_behavior`: 첫 실패 단계가 어떻게 실패했는지다. `rejected`(5xx·429로 바로 거절), `timed_out`(5초 안에
  답하지 못함), `connection_failed`(연결 거부·끊김) 중 가장 많은 쪽이 `kind`다. 바로 거절하는 서비스는 부하가
  빠지면 돌아오지만, 요청을 붙잡고 있다가 시간 초과로 끝나는 서비스는 사용자 전원을 기다리게 한다. 고칠 곳이 다르다.
- `targetUsers`(요청 필드, 1~500): 요청자가 예상하는 동시 사용자 수. 이 인원을 단계에 꼭 넣어 정확히 잰다(예: 150이면
  10·50·100·150·200). `target_met`은 그 인원까지 모든 단계가 실패 없이 답했는지다. 기준을 요청자가 정했으므로 AI는
  `target_met: false`를 Fail로 판정할 수 있다. 값이 없으면 `target_met`도 `null`이고 측정값만 낸다.
- 부하가 끝난 뒤 다시 정상 응답하는지 확인한다. 돌아오지 않으면 `recovered: false`로 실패다. 부하 중 앱
  컨테이너가 재시작했으면 `restart_count_delta`에 남는다.
- 부하 요청 수는 `metrics.probe_count`에 넣지 않는다. 묶음 가용성은 장애 시나리오와 같은 확인 요청으로만 계산한다.
- `suite_standard`와 `suite_deep`의 첫 시나리오다. 장애를 넣기 전 깨끗한 배포에서 재야 용량이 섞이지 않는다.

부하 Pod의 CPU가 모자라면 서비스가 느린 것처럼 보일 수 있다. 로컬 측정(4코어 VM, CPU 상한 없음)에서 이 생성기는
400명(초당 약 340건)을 보냈다. 그보다 큰 단계는 생성기 한계부터 확인해야 한다.

## 현재 검증 범위와 후속 작업

- 유효한 실제 관측 샘플 12종: [샘플 및 출처](../data/chaos/runtime-kind-2026-10-07/README.md).
- DB/Redis 사용자 업무 장애는 Frontend 프록시 → Backend → Redis → AI → Sandbox → Redis → Backend로 검증했다.
- 브라우저 클릭/화면 동작 자동화는 검증하지 않았다. 프록시 HTTP와 프론트 빌드·테스트를 검증했다.
- deep 8종을 QuickByte에 한 번 배포한 환경에서 순차 실행·복구·정리하는 전체 경로를 검증했다.
- 현재 API 프로세스는 동시 실험을 lock으로 제한한다. 다중 Worker/동시 사용자 격리·스케줄링은 별도 단계다.
- 임의 레포의 Dockerfile/Compose/Kubernetes 자동 분석은 아직 없다. 등록 profile 또는 레포 설정이 필요하다.
- 다른 OS·클러스터의 실측은 별도 필요하다. 이번 runtime fault 검증은 로컬 kind 기준이다.
- AI의 시나리오별 판정 정책, Prometheus 사용량 수집, 이미지 캐시/TTL 회수, CI/CD는 후속 범위다.

## 최종 standard 연동 실측 (2026-10-07)

Backend task: `f818baef-b393-41ae-af30-abc7639f3849`

QuickByte commit `d1c5c5edd86791bcec3ccbf93abde42d6778c3c3`를 새 요청 namespace에 배포했다.
`suite_standard`의 Container Kill → CPU Stress → Network Latency가 모두
`observed`, `exitCode: 0`으로 완료됐으며 누락 시나리오는 없다.

| 시나리오 | 실패/전체 HTTP 요청 | p95 ms |
|---|---:|---:|
| Container Kill | 35/75 | 61.51 |
| CPU Stress | 0/47 | 34.39 |
| Network Latency | 0/44 | 626.45 |

최종 Backend 상태는 `FAILED`다. 인프라/전송 실패가 아니라 현재 AI Judge의
`chaos_recovery_exceeds_expected_bound` 판정이다: 관측 복구 90.08초 > 설정 기반 기준 65초.
Sandbox의 실험 완료와 서비스 SLO 합격을 혼동하지 않는다. AI 판정 코드는 변경하지 않았다.

`source.artifact_cleanup`에서 namespace, host image, kind node image 삭제가 모두 true,
errors는 비어 있다. 실제 namespace와 host image가 존재하지 않는 것도 별도로 확인했다.
실제 LLM 생성 대신 명시적인 `-DisableLlm` 설정으로 규칙 판정·fallback 보고서를 사용했다.
전체 실행 응답은 로컬 `.runtime/evidence/frontend-backend-standard-final.json`에 보존한다.

이번 최종 테스트: Sandbox 28개, Frontend 4개·production build 통과,
Backend 41개 실패/오류/skip 0건. 프론트 lint는 기존 UI Fast Refresh 경고 2개다.

## 정밀 검사 8종 연속 실측 (2026-10-07)

Task `f778fe57-9a96-469f-babc-c476c4f56cb5`, 같은 QuickByte commit을 한 번 clone·배포했다.
전체 Sandbox 응답 소요 시간은 914,625ms(약 15분 15초)다. 아래 8종이 모두
`observed / exitCode: 0 / recovered: true`이며 `not_executed`는 비어 있다.
모든 시나리오의 probe transport는 `in_cluster_http`다.

| 순서 | 시나리오 | 실패/전체 요청 | p95 ms | 복구 초 |
|---|---|---:|---:|---:|
| 1 | Container Kill | 33/75 | 47.82 | 82.60 |
| 2 | Pod Delete | 31/66 | 31.14 | 76.90 |
| 3 | CPU Stress | 0/47 | 32.98 | 0 |
| 4 | Network Latency | 0/44 | 612.18 | 0 |
| 5 | Packet Loss | 1/44 | 1444.60 | 2.41 |
| 6 | Service Blackhole | 16/31 | 16.75 | 13.91 |
| 7 | Scale Down | 101/116 | 1032.07 | 95.98 |
| 8 | Rollout Restart | 1/101 | 54.77 | 1.85 |

Scale Down은 replica 1 → 0 → 1을 복원하고 HTTP 연속 성공을 확인했다.
Rollout은 실제 새 Pod UID로 교체됐고 HTTP 연속 성공을 확인했다.
기존 앱 Pod 직접 port-forward 관측을 in-cluster Service HTTP로 보강했다.

Backend 판정은 `FAILED`: `chaos_recovery_exceeds_expected_bound`, 95.98초 > 현재 기준 65초.
실험 복구 성공을 SLO 합격으로 바꾸지 않았으며 AI 정책은 수정하지 않았다.
namespace·host image·kind image 정리 report는 모두 성공, errors는 비어 있고 실제 제거도 확인했다.

### 최종 보고서 DB 저장 보강

정밀 검증에서 Redis 캐시의 최종 결과와 달리 PostgreSQL JSONB 저장이 실패하는 문제를 발견했다.
Server `TaskStatusPgRepository`의 보고서 파라미터에 `CAST(? AS JSONB)`를 적용했다.
실제 PostgreSQL 17 테스트에서 오류를 재현하고, 수정 후 전체 41개 테스트를 통과했다.
캐시 조회만 성공하면 DB 저장도 성공했다고 간주하지 않는다.
기존 실제 실험 결과를 AI SQLite에서 읽어 동일 최종 이벤트로 재전달했다.
이는 저장 경로 재검증이며 Chaos 재실행이나 AI 판정 재생성이 아니다.
로컬 재전달로 모니터링 카운터가 중복 집계될 수 있으므로 이번 카운터를 운영 통계로 쓰지 않는다.
`verify_backend_pipeline.py --verify-postgres`는 최종 상태·observed·exit code·시나리오 수를 DB와 대조한다.
직접 DB 조회 및 이 도구의 확인 결과: DB/Redis PASSED·2종, standard FAILED·3종,
deep FAILED·8종의 JSONB 보고서와 최종 상태가 모두 저장됐다.
재전달 후 DB의 updated_at은 재전달 시각이다. 실험 소요 시간은 원래 execution_result.duration_ms를 사용한다.
