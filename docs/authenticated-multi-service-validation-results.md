# 인증 업무·5개 앱 검증 결과 (2026-11-05~06)

## 범위

- 실제 공개 레포: `prgrms-be-adv-devcourse/beadv7_7_GrafanaOn_BE`, commit `4062b36d406d8ab30d97aa658b6e0d30ffc8dacc`.
- Windows + Docker Desktop + 기존 `kind-codereferee`에서 실행했다. AWS/Mac 실측이나 Frontend→Backend→AI 전체 E2E가 아니다.
- 로컬 재현 설정으로 gateway / identity / commerce / financial / recommendation의 **5개 앱**과 PostgreSQL을 배포했다. 제품에 해당 레포명·API 경로 분기를 추가하지 않았다.
- 실제 관측 업무는 회원가입의 gateway → identity → financial 지갑 생성 → DB와 로그인·프로필 조회의 gateway → identity → DB다. 회원가입·로그인 후 인증 토큰으로 프로필을 조회하고 JSON 결과를 확인했다. 결제·검색·추천·외부 S3/AI 연동은 검증하지 않았다.
- 동일 원본 clone의 루트 빌드/테스트가 통과한 뒤 실제 production 배포 단계 함수를 실행했다. 외부 HTTP URL 요청부터 모든 단계를 다시 실행한 결과와 구분한다. 재현용 Dockerfile은 실제 빌드된 JAR를 패키징한다.

## 빌드·테스트

- 원본 레포 테스트 **510개 통과**, 실패/건너뜀 0. 모듈별 commerce168 / financial146 / gateway17 / identity115 / recommendation47 / common17.
- 전체 검사 약 2,213초. 이 진단 실행에만 `timeout=3600`을 명시했고 제품 검사 기본값600초·API 배포 단계1200초는 유지했다.
- 일반 PostgreSQL로 실행한 앞선 결과는 493개 중7개 실패였다. vector 확장이 필요한 테스트였으며 이를 숨기거나 제외하지 않았다. 고정 pgvector 이미지 계약을 명시한 뒤 원본 전체 테스트를 다시 수행했다.
- 검사 DB와 검사 캐시 회수 성공. 앱별 설정은 CPU1 / memory768Mi, DB256Mi이며 앱의 모든 업무 용량을 보장하는 사양은 아니다.

## 실제 장애·부하

12종의 최종 실행은 모두 `observed / exitCode 0`이다. 이는 실험 실행과 업무 복구 근거이며 AI Judge의 최종 PASS를 직접 검증한 것은 아니다.

| 시나리오 | 최종 실행 상태 / exitCode | 실패한 업무 표본 | 복구(s) |
| --- | --- | ---: | ---: |
| `load_concurrent_users` | observed / 0 | 0 | 0.0 |
| `litmus_container_kill` | observed / 0 | 38 | 102.77 |
| `litmus_pod_delete` | observed / 0 | 46 | 132.59 |
| `litmus_pod_cpu_hog` | observed / 0 | 0 | 0.0 |
| `litmus_pod_network_latency` | observed / 0 | 1 | 2.64 |
| `litmus_pod_network_loss` | observed / 0 | 1 | 3.61 |
| `service_selector_blackhole` | observed / 0 | 0 | 0 (최종 코드 재실행) |
| `deployment_scale_down` | observed / 0 | 165 | 109.33 |
| `rollout_restart` | observed / 0 | 1 | 2.0 |
| `litmus_pod_memory_hog` | observed / 0 | 0 | 0.0 |
| `litmus_pod_memory_oom` | observed / 0 | 191 | 142.71 |
| `dependency_database_outage` | observed / 0 | 6 | 16.03 |

- OOM: identity 앱 컨테이너 `OOMKilled / exit137`, restart0→1, `oom_observed=true`, 정확한 앱 cgroup 대상 근거를 확인했다. stress helper만 종료된 것을 앱 OOM 성공으로 세지 않았다.
- Scale Down: 1→0→1, `zero_pods_observed=true`, 실제 새 Pod UID 확인. 2개 중1개 대상 지정은 아래 별도 구성으로 검증한다.
- HTTP 실패0은 장애가 없었다거나 무중단을 보장하지 않는다. CPU/메모리 압박과 라우팅 단절에서 실패가 표본에 잡히지 않은 경우도 그대로 기록한다.
- 라우팅 단절의 기존 결과14.46초는 전체 소요시간이었다. 복구 시간과 실험 소요시간을 분리한 최종 코드로 같은 배포에서 다시 실행해 실패0 / 복구0을 확인했다. 원본 JSON은 변경하지 않았다. Service endpoint가 없어도 기존 연결을 통해 요청이 성공할 수 있으므로 주입과 업무 영향을 구분한다.

### 부하 측정

- 동시 요청1 / 5 / 10 / 20, 각10초, think1초. 한 인증 세션을 공유했으며20명의 독립 로그인 사용자가 아니다.
- 요청10 / 50 / 100 / 200, 전부 HTTP200. 미참여/미완료0, 측정 유효성 및 목표20 충족을 확인했다.
- p95 약194 /315 /419 /563ms, 처리량 약0.96 /4.70 /9.11 /18.22 workflow/s.
- 최종 지연·거부 분류 코드로 같은 배포에서1/5 동시 요청을 추가 실행했다. HTTP401/JSON 거부는 성공/연결 실패로 섞지 않는 별도 HTTP 회귀도 수행했다.

### 준비 실패와 재시도

- 초기 baseline 준비1회와 Pod Delete / CPU Hog / Rollout Restart / Memory Hog / DB 장애의 준비 단계에서 각1회 회원가입 HTTP503이 있었다. 장애 주입 전 observer setup에서 멈췄으며 원본 실패를 별도 보존했다.
- 앱 Ready 및 활성 장애 없음을 확인한 뒤 사람이 해당 준비 단계를 한 번씩 다시 실행했다. 새로운 인증 세션을 사용했다. 최종12종 완료는 **수동 준비 재시도가 포함된 결과**이며 한번에 무오류로 완주한 실행이나 제품 자동 재시도 정책이 아니다.
- 당시 결과에는 준비503의 상태 코드까지 기록했다. 11월6일 추가 진단에서 financial 지갑 호출의 TimeLimiter timeout을 재현했고 설정을 명시한 비교와 별도 준비 제한을 검증했다. 추가 실행 범위는 [인증 준비 분석](authentication-preparation.md)에 기록한다.

## 정리

- 원래60초 namespace 삭제 제한에서 시간 초과가 있었다. 직후 실제 namespace가 삭제됐음을 확인했고, 보존한 최초 결과의 `namespace_removed=false`를 성공으로 덮지 않았다.
- 후속 회수에서 해당 요청 이미지5개 모두 host/kind 삭제 및 오류0 확인. 작업 폴더도 삭제됐다.
- 별도 5개 앱 구성에서도 `--wait=true`를120초로 늘리는 것만으로 해결되지 않았다. 삭제 요청 후 namespace의 실제 부재를 조회하는 공통 함수를 API/CLI에 연결했다. 최대120초와 하위 호출 상한15초를 적용한다. 삭제 확인 뒤 이미지를 회수한다. 기한 뒤에도 남은 자원의 자동 재회수는 후속 과제다.

## 별도 회귀와 제한

- 별도 Python 앱5개/Redis 구성에서 실제 Git clone → 공통 테스트 → 이미지 빌드·적재·배포를 수행하고 FastAPI TestClient로 실제 API 경로와 실험 subprocess를 호출했다. 업무 경로는 gateway→api→edge→worker→storage→Redis이며 재현용 토큰을 사용했다.
- 인증 부하1/2 동시 요청, Service selector 단절, Container Kill, Redis 장애가 모두 `observed / exitCode0`이다. 업무 실패 표본은 각각0 /5 /2 /5개였다.
- Container Kill은 복제본2개 중 선택1개와 `reported_pod_scope_matches=true`를 확인했다. Redis 장애도 업무 실패와 복구를 확인했다.
- 이 구성의 최초 정리 결과는 `cleanup_incomplete`로 보존했다. namespace 부재 확인 후 이미지5개를 별도 회수했다.
- 최종 공통 정리 함수는 새 요청 이미지와 Pod5개로 별도 실측했다. 한 Pod에65초 preStop을 넣었고 API 정리 함수가93.68초에 namespace 부재를 확인했다. host/kind 요청 이미지5개 회수와 오류0을 확인했다. 이 추가 실행은 삭제·회수 경로 검증이다.
- 최종 Windows 네이티브 회귀293개 중292개 통과, POSIX 권한 테스트1개 건너뜀. Linux 컨테이너 전체305개 통과이며 Bash12개와 해당 권한 테스트를 포함한다. Linux용 테스트 복사본의 셸 파일은 LF로 실행했다.
- 앞선 전체 Windows 실행에서 POSIX12개 중11개가 Windows 경로/CRLF 조건으로 실패했다. 별도 Linux 전체 실행으로 확인했다. Linux 테스트 드라이버의 import 경로와 git/ps 도구 준비 오류도 로그에 보존했다.
- 깨끗한 프로세스의 API import를 추가 확인해 scripts 경로 주입 없이 기동하도록 수정했고 회귀에 포함했다. Scale Down 복구 실패 단위 테스트에서 실제 kubectl 조회가 새던 부분도 대역으로 분리했다.
- 현재 AI 파서로 실제 레포12종과 별도 API 구성4종을 읽어 실행 결과, 공통 검사510개, 서비스5개 자원 설정·업무 진입점·장애 대상이 유지되는 것을 확인했다.
- 후속 통합 검증 대상은 Frontend/Backend 전체 경로, LLM 수정 생성, AWS/Mac 실측이다. 이번 실측은 Sandbox 실행과 AI 파서 호환까지다.

원본 결과·실패·재시도·로컬 재현 설정은 `tmp/real-repository-20261105`에 보존하고 커밋하지 않는다. 문서는 검증 요약이며 전 기능/전 보안/모든 임의 레포의 성공 보장이 아니다.
