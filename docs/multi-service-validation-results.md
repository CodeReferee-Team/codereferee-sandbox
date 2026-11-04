# 멀티서비스 검증 기록

2026-11-04, `feat/multi-service-validation`의 로컬 검증이다.
사용법은 [멀티서비스 설정](multi-service-validation.md)을 참고한다.

## 실제 실행 범위

- Windows + Docker Desktop + 기존 `kind-codereferee` 클러스터에서 실행했다.
- 로컬 Git 테스트 레포를 실제 clone하고 Docker build → kind image load → Kubernetes 배포 → HTTP 관측 → 장애 → 복구 → 정리를 실행했다.
  테스트 드라이버가 clone URL만 로컬 Git으로 대체했다. GitHub 접근과 FE/BE/Redis 큐/AI 전체 E2E를 재실행한 것은 아니다.
- 앱 2개(gateway → API)와 실제 Redis·PostgreSQL을 연결했다. 업무 경로는 두 의존성에 실제 요청한다.
- gateway 256Mi, API 128Mi 조건에서 정밀 묶음 9종을 연속 실행하고 추가 4종도 같은 배포에서 실행했다.
- 아래 13종 모두 `observed`, `exitCode: 0`이었다. 이는 실험·복구 관측 결과이며 AI Judge의 최종 SLO 합격을 뜻하지 않는다.

| 시나리오 | 실패 HTTP / 전체 표본 |
| --- | ---: |
| 동시 사용자 부하 | 0 / 6 |
| Container Kill | 2 / 47 |
| Pod Delete | 1 / 43 |
| CPU Hog | 0 / 24 |
| Network Latency | 1 / 23 |
| Network Loss | 2 / 24 |
| Service Selector Blackhole | 8 / 14 |
| Deployment Scale Down | 15 / 21 |
| Rollout Restart | 0 / 10 |
| Memory Hog | 0 / 25 |
| 앱 OOM | 5 / 19 |
| PostgreSQL 의존성 장애 | 5 / 28 |
| Redis 의존성 장애 | 5 / 28 |

부하 행의 표본은 기본 baseline/복구 관측이며 사용자 부하 단계의 요청 통계는 별도 결과에 있다.
부하는 2·5명 각 2초, Litmus 장애는 5초, baseline은 주로 3회인 단축 기능 검증이다.
의존성 장애는 15초다. 200명 처리 능력이나 운영 SLO를 검증한 결과가 아니다.
묶음에는 3600초 기한을 사용했으며 운영 기본 기한 안에 완료된다는 보장도 아니다.
실패 표본 0은 그 실행에서 중단을 포착하지 못했다는 뜻이며 무중단 보장은 아니다.

## 복제본과 실제 장애 확인

### 최대 4개 앱 실제 실행

gateway → edge → API → storage → 실제 Redis로 연결한 별도 로컬 Git 레포도 실행했다.
업무 앱 4개 모두 메모리 256Mi로 선언했다.

- storage → API → edge → gateway 순서로 실제 배포하고 최종 업무 HTTP 요청 성공을 확인했다.
- 중간 API의 Service selector 단절 및 Pod 1개 Container Kill을 주입했다.
  gateway를 통한 업무 실패·복구와 `observed / exitCode 0`을 확인했다.
- 별도 patchDiff로 중간 edge 앱을 종료 코드 23으로 부팅 실패시켰다.
  실제 CrashLoopBackOff와 `failed_service: edge`, Pod 진단을 확인했다.
- 정상·실패 실행 모두 namespace, 요청 이미지 4개(호스트·kind), workspace 정리를 확인했다.

이후 4개 앱에서도 전체 13종을 별도로 실행했다. 추가 실측 표는 이 문서 마지막 절에 기록한다.

### 복제본 변경

동일한 Container Kill에서 정확히 Pod 1개를 선택하도록 설정했다.

| API 복제본 | 설정 적용 | 실패 HTTP / 전체 | 복구 시간 |
| --- | --- | ---: | ---: |
| 1 | 원본 | 1 / 47 | 2.67초 |
| 2 | 실제 patchDiff 적용 | 0 / 46 | 0초 |

2개 실행에서는 선택되지 않은 Pod의 UID가 유지되고 Ready·restart 0임을 확인했다.
Scale Down은 명령 수락만 확인하지 않고 **2 → 실제 Pod 0개 → Ready 2개** 복원과 HTTP 실패를 확인했다.
짧은 장애에서 Pod가 내려가기 전에 복원될 수 있는 기존 경로를 수정했다.

## 실패를 성공으로 바꾸지 않기

- 최초 64Mi gateway 실행에서 Memory Hog 도중 gateway가 실제 OOMKilled/137로 종료됐다.
  이 실행은 실패 기록으로 보존했다. 메모리 한도를 바꾼 이후 성공과 구분한다.
- 이는 이번 검증을 위해 만든 테스트 앱이며 팀원의 chaos-demo에서 발견한 결함이 아니다.
  메모리 누수·요청 누적·한도 중 무엇이 원인인지는 확정하지 않았다.
- 의도한 앱 OOM 시나리오에서는 `oom_observed`와 대상 컨테이너 OOM 증거를 확인했다.
- patchDiff로 두 번째 앱의 부팅을 실패시켜 서비스 이름·Pod 진단을 정리 전에 확보하고 모든 요청 이미지를 회수했다.
- 런타임 실패 시 다른 업무 앱의 구조화된 Pod 상태도 보내도록 보강했다. 해당 경로의 로그·자유 텍스트는 보류한다.

## 관측 데이터와 호환성

- 서비스별 자원 한도와 값의 출처를 구분한다. 별도 실제 Pod 조회에서 선언된 limits와 일치함을 확인했다.
- 현재 AI 응답 파서로 실제 13개 결과를 읽어 `source`와 `sandboxReport`가 유지됨을 확인했다. LLM 호출은 하지 않았다.
- 서비스별 PromQL 범위와 UID 분리는 테스트 대역으로 확인했다. 이번에는 외부 Prometheus 수신기까지의 자원 지표 E2E를 실행하지 않았다.
- namespace·호스트 이미지·kind 노드 이미지·작업 폴더를 정리했다. 같은 이미지의 다른 태그가 이미 제거된 경우도 정상 회수로 처리한다.
- 최종 Windows 회귀 테스트 255개 중 254개 통과·1개 플랫폼 전용 테스트 제외, Linux 선택 회귀 63개 통과다.
  Linux에는 Bash bootstrap 12개와 Windows에서 제외된 파일 권한 경로가 포함된다. 전체 Linux 회귀를 실행했다는 의미는 아니다.
- 3개 구성은 배포 순서·이미지 수를 테스트 대역으로 확인했다. 4개 구성의 정상 순서·중간 롤아웃 실패·전체 이미지 회수와
  5개 이상 거부도 회귀 테스트에 포함했다.
- Agent 수집 필터도 확인했다. 멀티서비스는 요청 namespace를 수집하고 앱별 조회로 분리한다.
  기존 단일 앱은 기존 Deployment 필터를 유지한다.

## 후속 논의: 조건을 바꾸는 재검증

아래는 **미구현 정책 제안**이다. AI 팀과 횟수·시간 예산을 합의해야 한다.

- 수정 효과 확인은 같은 부하·검사 조건에서 수행한다. 기존 AI 수정 루프의 최대 3회 및 전체 기한과 함께 관리한다.
- 원인 구분을 위한 조건 변경은 선택적으로 추가 1회부터 검토한다. 부하 또는 메모리 한도 하나만 바꾼다.
- 조건을 완화한 성공을 원래 조건의 합격으로 덮지 않는다. 불명확하면 원인 미확정으로 종료한다.
- 장시간 누수 검사는 별도 정밀 검사로 검토한다. Sandbox는 사실과 관측 데이터를 제공하고 원인 진단·수정은 AI가 담당한다.

## 이번 범위 밖

멀티레포 그룹, 여러 EC2 배정, EC2 자동 기동·종료, 여러 Compose 앱의 자동 변환,
정확한 사전 실행 시간 예측, AI의 version 2 수정 정책, AWS 배포 및 프론트부터의 전체 자가치유 시연은 별도 작업이다.

## 앱 4개 전체 13종 추가 실측

gateway → edge → API → storage를 연결하고 API가 실제 Redis·PostgreSQL에도 접근하는 구성이다.
4개 업무 앱 모두 256Mi이며, 이전 2개 앱 기록과 별도로 실행했다. 13종 모두 observed / exitCode 0, 미실행 없음이다.

| 모드 | 실패 HTTP / 전체 |
| --- | ---: |
| load_concurrent_users | 0 / 6 |
| litmus_container_kill | 2 / 45 |
| litmus_pod_delete | 3 / 39 |
| litmus_pod_cpu_hog | 0 / 23 |
| litmus_pod_network_latency | 1 / 21 |
| litmus_pod_network_loss | 1 / 23 |
| service_selector_blackhole | 8 / 14 |
| deployment_scale_down | 12 / 18 |
| rollout_restart | 0 / 10 |
| litmus_pod_memory_hog | 0 / 24 |
| litmus_pod_memory_oom | 5 / 18 |
| dependency_database_outage | 6 / 27 |
| dependency_redis_outage | 5 / 27 |

DB·Redis 장애 때 업무 HTTP 실패가 실제 발생했고, 앱 OOM은 대상 컨테이너 증거를 확인했다.
4개 앱의 실제 묶음 응답도 현재 AI 파서로 읽어 서비스별 자원 출처·업무 진입점·장애 대상 필드 보존을 확인했다.
namespace·이미지 4개·workspace 정리를 확인했다. 별도 중간 앱 기동 실패 검증도 앞 절의 기록대로 완료했다.
처음에는 검증 드라이버의 12초 DB 준비 대기가 부족해 준비 단계 infrastructure_error로 중단됐다.
이 실패 및 전체 회수 기록을 보존하고, 운영 기본 240초보다 짧은 120초 준비 대기로 새로 실행한 결과가 위 표다.
제품의 시간 상한을 늘린 수정은 없다. 단축 부하·장애 시간, 테스트 기한 3600초 및 기존 검증 경계는 그대로 적용한다.
