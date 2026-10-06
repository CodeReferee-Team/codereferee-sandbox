# Runtime Chaos 시나리오와 검사 묶음

## 실행 범위

kind + Litmus에서 사용자 레포를 한 번 clone/build/배포한 뒤 선택한 시나리오를
순서대로 실행한다. 각 시나리오는 건강한 baseline을 확인한 후 주입하고, 해제 후
복구를 확인한다. 실패 또는 인프라 오류가 나면 다음 시나리오는 실행하지 않고
`not_executed`에 기록한다. 요청 namespace는 마지막에 정리한다.

새로 지원하는 `chaosMode`:

| 모드 | 주입 | 관측 |
|---|---|---|
| `litmus_container_kill` | 컨테이너 종료 | 같은 Pod UID, restart 증가, HTTP 복구 |
| `litmus_pod_cpu_hog` | 1 CPU worker, 80% load, 15초 | HTTP 응답과 fault revert |
| `litmus_pod_network_latency` | eth0 지연 300ms, 15초 | cluster 내부 HTTP 지연 |
| `litmus_pod_network_loss` | eth0 packet loss 50%, 15초 | 실제 요청 실패 또는 지연 |
| `litmus_pod_memory_hog` | 컨테이너 memory limit의 20%, 15초 | HTTP 및 종료 상태 |
| `litmus_pod_memory_oom` | memory limit의 125%, 15초 | 이번 실험 중 OOMKilled 여부 |
| `dependency_database_outage` | 선언된 DB Pod의 packet loss 100%, 15초 | 프로필의 업무 API |
| `dependency_redis_outage` | 선언된 Redis Pod의 packet loss 100%, 15초 | 프로필의 업무 workflow |

메모리 실험은 컨테이너 limit이 32Mi..4Gi로 명시된 경우만 허용한다.
한도 초과 실험을 실행했다고 반드시 대상 앱이 OOMKilled가 되는 것은 아니다.
`oom_observed`는 재시작 증가와 이번 실험 이후의 종료 시각·사유가 확인된 경우만 true다.

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
  --output .runtime/evidence/full-pipeline.json
```

Frontend 5173, Backend 18090, Sandbox 8100을 사용한다.
Frontend 프록시는 `CODEREFEREE_BACKEND_URL`로 변경 가능하다.
Windows Docker의 예약 포트 때문에 8080이 bind되지 않는 환경을 고려했다.
`-DisableLlm`은 실제 모델 호출 없이 규칙 판정과 fallback 보고서로 전송·실행 경로를
검증한다. 실제 LLM의 설명·패치 생성 품질 검증은 별도다.

동시 사용자 부하 테스트는 현재 실험에 포함하지 않는다. 낮은 요청량에서 장애를
주입한 결과이며 처리 용량이나 운영 환경의 안정성을 보장하는 결과로 해석하지 않는다.

## 현재 검증 범위와 후속 작업

- 유효한 실제 관측 샘플 10종: [샘플 및 출처](../data/chaos/runtime-kind-2026-10-07/README.md).
- DB/Redis 사용자 업무 장애는 Frontend 프록시 → Backend → Redis → AI → Sandbox → Redis → Backend로 검증했다.
- 브라우저 클릭/화면 동작 자동화는 검증하지 않았다. 프록시 HTTP와 프론트 빌드·테스트를 검증했다.
- deep 8종 묶음 전체를 한 요청으로 실행한 최종 검증은 아직 하지 않았다. 개별 실행과 묶음 결과를 구분한다.
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

이번 검증: Sandbox 단위 테스트 25개, Frontend 4개·production build 통과,
Backend 전체 테스트 40개 실패/오류 0건. 프론트 lint는 기존 UI Fast Refresh 경고 2개다.
