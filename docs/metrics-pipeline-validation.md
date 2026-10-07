# 메트릭 파이프라인 실측 (2026-10-08)

Windows + Docker Desktop + 기존 kind에서 실행했다. Frontend main `3e0978d`, Backend dev `cdb12c7`,
AI main `a040a69`, 최신 Sandbox dev 기반 메트릭 작업 브랜치로 실제 연동했다. Mock AI/LLM 호출은 사용하지 않았다.
Frontend 5174의 실제 Vite 프록시→Backend 8080→Redis→AI Worker→Sandbox 8122→Redis→Backend→PostgreSQL 경로다.
브라우저 클릭 자동화 검증이 아니라 실제 HTTP 제출·폴링이다. 로컬 receiver는 19091이다.

| 실제 레포 | task ID | 관측/메트릭/정리 | BE 최종 상태 |
| --- | --- | --- | --- |
| QuickByte Demo (`d1c5c5edd867`) | `804d82f7-0cc2-48e7-9bbb-3872a3fc9f80` | Pod Delete, UID 2개, CPU·메모리 received, DB 보존, 앱·관측 namespace 및 host/kind 요청 이미지 제거 | FAILED: 복구 95.07초 > AI 기준 65초 |
| Docker getting-started-app (`6b025fc53bc7`) | `0c29b1d4-3a17-459b-a94d-dbd3d49c16b8` | URL 자동 탐색·build·Pod Delete, UID 2개, 메트릭/DB/정리 전체 확인 | PASSED |

QuickByte 전체 API 980.050초, Node 339.226초. 기존 AI HTTP 기본값 600초로 QuickByte의 이 cold 경로는
완주할 수 없어 실측 Worker에 `SANDBOX_HTTP_TIMEOUT_SECONDS=1800`을 사용했다. 기본 코드/AI 정책은 바꾸지 않았다.
동기 long-running 요청·빌드 중복·toolchain cache 개선은 후속이며 내일 데모 설정에 이 제한을 반영해야 한다.
Node의 test_execution은 `not_declared`이다. SRE Pass를 단위 테스트/커버리지 보장으로 해석하지 않는다.

## 보존 검증에서 찾은 조회 문제

QuickByte는 종료 한 점의 instant query가 일시 scrape 공백에 걸려 처음 보존 확인이 실패했다.
데이터를 잃은 것이 아니었으며 동일 receiver의 과거 구간에서 CPU·메모리 원본 샘플 각각 138개를 확인했다
(진단 150초 창). 재제출 없이 저장된 원본 결과를 실제 receiver/DB에 대조했고 모든 검증 항목이 통과했다.
보존 verifier를 기록된 구간 `count_over_time`으로 고쳤으며 1초 평가점 수를 실제 수신 샘플 수로 주장하지 않는다.

## 수집 실패

`metrics-receiver-failure-022`: 별도 프로세스에서 remote_write 주소를 연결 불가 localhost:1로 설정했다.
Agent Ready만으로 수신 성공이라고 처리하지 않고 warmup 실패 `status: error`를 기록했다.
관측 namespace는 실제로 제거됐다. 정상 receiver/API 환경은 바꾸지 않았다.
이 보고서는 별도 메트릭 관측 상태이며 사용자 프로세스 exitCode를 생성하지 않는다.

## Node 설치 환경 수정

첫 Node 요청 `b545e783-d158-48ab-9213-1923978730e4`는 설치에서 exit 7로 실패했다.
root archive ownership 복원 중 `lchown EPERM` 후 node-gyp fallback 오류였고 성공 검증으로 세지 않았다.
Node 코드에 추가 권한을 주지 않고 uid 1000·cap-drop ALL로 실행하며, readonly clone을 일회성 Linux volume에
복사해 단계 간 의존성을 유지하도록 고쳤다. 신뢰된 초기화만 새 volume에 한정된 CHOWN 권한을 사용한다:
제출 소스 mount·network·사용자 명령이 없으며 CPU/메모리/pids 및 시간을 제한한다.

비특권 bind 방식만으로 Windows 설치가 성공하는 것도 확인했지만 Linux의 소스 디렉터리 소유자 차이를 피하기 위해
최종적으로 native 작업 공간을 사용했다. 초기 빈 volume 소유권 재초기화로 copy가 실패한 사례도 확인하고
실제 subdirectory를 만들어 초기화하도록 수정했다. 최종 Node 실제 install 및 전체 E2E가 성공했다.
clone/cache 정리도 확인했다. Node 수정은 별도 `fix:` 기능 커밋이다.

## AI #99 조회 호환 확인

변경 없는 PR #99의 PromQL을 실제 receiver에 적용했다. 조회 HTTP 시각만 종료 시각으로 고정한 진단 replay다.

| 요청 | CPU cores × 100 | memory MB (decimal) |
| --- | --- | --- |
| QuickByte | 107.9567 | 374.181888 |
| Node | 14.794947 | 76.578816 |

이는 label/metric 조회 호환 확인이며 #99의 현재 시각 조회 workflow나 SLO 정책의 검증이 아니다.
CPU limit 대비 비율로 해석하면 안 된다. AI source·Judge·모델은 수정하지 않았다.

원본은 로컬 `.runtime/evidence/metrics-repository-e2e-018.json`, `019.json`, `021.json`,
`node-nonroot-install-020.json`, `metrics-receiver-failure-022.json`, `ai-prometheus-contract-023.json`에 보관한다.
원본 보고서/임시 프로세스 설정은 커밋하지 않는다. receiver 준비는 기존 영속 receiver 재사용으로 실측했고
새 머신에서 Compose 생성 및 Mac/Linux 실행은 추가 검증이 필요하다.

## 남은 범위

- API 병렬 worker/물리 자원 격리, 멀티서비스/멀티레포 그룹, 강제 종료 TTL/GC.
- 모든 미전송 WAL drain 및 관측 구간 전체 무누락의 증명.
- AI #99 머지·정확한 평가 시각/분모/재검증 구간 계약 후 자원 SLO 최종 입력 연동.
- 사용자 앱 distributed trace와 수신 인증/TLS/클라우드 네트워크/CD는 이번 PR 범위 밖.
