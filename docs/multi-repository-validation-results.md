# 멀티레포 검증 결과

2026-11-11~12 Windows + Docker Desktop + 기존 kind-codereferee 클러스터에서 수행했다. 기능 사용법은 [설정 파일 기반 멀티레포 검증](multi-repository-validation.md)을 따른다.

## 이번에 확인한 흐름

기준 레포의 version 3 설정을 읽고 추가 레포를 각각 복제한 뒤, 서비스별 검사·이미지 빌드·배포를 수행했다. 실제 Chromium으로 FE에서 데이터를 입력하고 FE 프록시 → API → PostgreSQL 저장·조회까지 확인했다. 3개·5개 앱 구성에서는 API가 추가 서비스에도 요청하도록 구성했다.

검증 대상은 서로 분리해 커밋한 로컬 Git fixture다. 프로세스에만 적용한 Git URL 재작성으로 GitHub 형식 URL을 해당 fixture로 연결했다. 실제 Git clone·커밋 확인과 Docker·Kubernetes 실행을 사용했다. 공개 GitHub 서비스의 네트워크·인증 환경은 별도 확인이 필요하다.

초기 Sandbox API 검증은 TestClient로 호출했다. 현재 AI 파서에 실제 응답 7건을 입력해 필드 보존도 확인했다. 이후 11월 12일 실제 Backend → Redis → AI → Sandbox → Redis → Backend 전체 큐 연동을 추가 검증했다. 결과는 아래 추가 검증 항목에 정리했다.

## 정상 구성과 장애·부하 실측

| 구성 | 실제 실행 | 결과 | 브라우저 업무 확인 |
| --- | --- | --- | --- |
| FE·API 각각 별도 레포, 앱 2개 + PostgreSQL | 부하 → Container Kill → Service selector 단절 → DB 장애 | 4종 모두 observed / exitCode 0 | 초기 및 각 시나리오 후 저장 행 수 1→2→3→4→5 |
| 별도 레포 3개, 앱 3개 + PostgreSQL | Container Kill | observed / exitCode 0, 복구 2.47초 | 저장 행 수 1→2, API가 추가 서비스 호출 |
| 별도 레포 5개, 앱 5개 + PostgreSQL | Service selector 단절 | observed / exitCode 0, 복구 11.22초 | 저장 행 수 1→2, API가 추가 서비스 3개 호출 |
| 설정 전용 기준 레포 + FE·API 레포, 앱 2개 + PostgreSQL | Service selector 단절 | observed / exitCode 0, 복구 11.76초 | 저장 행 수 1→2 |

앱 2개 구성의 Container Kill에서 HTTP 실패 2건·복구 5.12초, selector 단절에서 실패 15건·복구 11.70초, DB 장애에서 업무 요청 실패 5건·복구 12.43초를 확보했다. 이후 브라우저의 저장·조회도 다시 성공했다.

### 부하 단계별 결과

요청 목표 사용자는 5명으로 설정했다. 기존 부하 시나리오가 5·10·50·100·200명 단계를 실행했다. `target_met`는 true, `measurement_valid`는 true이며 `all_stages_clean`은 false다.

| 동시 사용자 | 업무 시도 | 실패 | 오류율 | p95 |
| --- | --- | --- | --- | --- |
| 5명 | 100 | 0 | 0% | 119.18ms |
| 10명 | 200 | 0 | 0% | 99.10ms |
| 50명 | 465 | 91 | 19.57% | 3269.53ms |
| 100명 | 609 | 120 | 19.70% | 4788.27ms |
| 200명 | 1179 | 971 | 82.36% | 4237.55ms |

이 구성의 관측상 첫 오류 단계는 50명이다. 실행 결과의 exitCode 0은 요청한 목표 부하와 실행 후 복구를 확인한 결과다. 높은 부하에서는 5xx와 시간 초과를 기록했다. 정확한 처리 용량과 자원 부족 원인은 호스트 부하·앱 설정을 통제한 추가 측정으로 확인해야 한다.

## 중간 실패와 분류

| 실패 조건 | 수정 후 응답 | 확인한 내용 |
| --- | --- | --- |
| 추가 레포가 존재하지 않음 | observed / exitCode 89, failed_repository=backend | 설정 보완 필요 결과로 반환, Chaos 미실행, 정리 완료 |
| API 이미지 준비 후 FE Dockerfile에서 exit 17 | observed / exitCode 1, failed_service=web, failed_repository=main | 먼저 로드한 kind 이미지까지 회수, Chaos 미실행 |
| FE 시작 명령 exit 17, rollout 단계 상한 15초 | infrastructure_error / exitCode null / timedOut true | Pod 진단 수집, failed_service=web, Chaos 미실행, 정리 완료 |

존재하지 않는 추가 레포가 처음에는 일반 실패로 반환돼 설정 오류 분류를 보완한 뒤 재실행했다. 빌드 실패에는 서비스와 레포 이름을 함께 반환하도록 보완하고 재실행했다. 최초 실행 결과도 로컬 기록에 보존했다.

rollout 실패 실측은 짧은 단계 제한이 걸린 실행이다. 실패 레포 매핑 보완 전 실행이므로 해당 실측의 응답에는 failed_repository가 없다. 보완 후 실패 레포 매핑은 빌드 실패 재실측과 회귀 테스트에서 확인했다.

## 커밋 근거와 재시도

- 정상 구성마다 선언한 레포 수·서비스 매핑·실제 Git SHA를 비교했다.
- `source.repositories`와 `source.service_repositories`가 현재 AI 파서를 통과해 유지되는 것을 실제 응답 7건으로 확인했다.
- 준비 재시도 시 이미 확인한 추가 레포 SHA를 다음 실행에 전달하고 해당 SHA를 checkout하는 경로는 회귀 테스트로 확인했다. 이번 실측에서 외부 브랜치를 이동시키며 재시도하는 실험은 수행하지 않았다.
- 기존 patchDiff는 기준 레포에 적용된다. 추가 레포를 지정하는 AI 수정·재검증 흐름은 후속 계약이 필요하다.

## 회귀 테스트

- Windows 358개 중 357개 통과, POSIX 권한 조건 테스트 1개 제외.
- Linux 370개 모두 통과. Linux 실행에는 POSIX bootstrap 테스트도 포함한다.
- Linux 최종 실행은 의존성을 설치한 임시 venv와 실행 가능한 tmpfs를 사용했다. 앞선 tmpfs noexec 및 pip target 방식의 테스트 환경 실패 기록은 보존하고 최종 통과 집계에서 제외했다.

## 자원 정리

정상·실패·재실행을 포함한 요청 9건의 자원 목록을 실제 클러스터·Docker 상태와 다시 대조했다. 9건 모두 다음 요청 자원이 남아 있지 않았다.

- 앱 namespace와 요청 관측 namespace
- 요청 전용 컨테이너·볼륨
- 요청 이미지의 호스트 태그와 kind 노드 이미지
- 요청 작업 폴더와 추가 레포 clone

공유 kind 클러스터·브라우저 실행 이미지와 기존 프로젝트 서비스는 보존했다. 이미지 태그 회수 후에도 Docker 빌드 캐시는 남을 수 있다.

초기 검증 중 Windows 디스크 여유 부족으로 다음 실행을 중단했다가 확인 후 이어서 수행했다. 프로젝트 전용 캐시 일부 정리와 첫 압축 후 여유가 약 0.9GiB여서 당시 추가 이미지 빌드를 중단했다. 이후 11월 12일 사용자 추가 승인으로 미사용 빌드 캐시 전체를 정리하고 오프라인 압축해 약 13.9GiB를 확보한 뒤 전체 큐 검증을 재개했다. 검증 이미지 재빌드 후 여유는 약 11.9GiB였다.

## 11월 12일 전체 큐 추가 검증

실제 Backend API 제출 → Redis 입력 큐 → AI Worker → Sandbox HTTP API → Redis 출력 큐 → Backend 조회와 PostgreSQL 저장을 확인했다. 현재 로컬 Backend 소스를 새로 빌드했고 Sandbox는 멀티레포 작업 브랜치, AI는 시간 제한·재시도 연동 브랜치를 사용했다.

| 요청 | Backend 최종 상태 | 확인한 내용 |
| --- | --- | --- |
| 기존 단일 레포 + Service selector 단절 | PASSED | 기존 실행·장애 경로 회귀 |
| 별도 FE·API 레포 + PostgreSQL | PASSED | 실제 Chromium 업무, 부하·Container Kill·selector 단절·DB 장애 4종 |
| 추가 레포 누락 | ERROR | Sandbox exitCode 89와 failed_repository 보존 |
| API 이미지 준비 후 FE 빌드 실패 | ERROR | Sandbox exitCode 1, failed_service=web / failed_repository=main 보존 |
| 일시 다운로드 연결 실패 | PASSED | 다운로드 3회 요청, 준비 단계 내 재시도 |
| 설치 단계 실제 시간 초과 | PASSED | 첫 시도 제한 12초 → 정리 → 두 번째 24초로 연장, 총 2회 실행 |
| 전체 시간 예산 소진 | ERROR | timedOut true, 추가 재시도 중단 사유와 정리 완료 |

설정 누락과 빌드 실패는 현재 AI 정책의 baseline_not_established에 따라 안정성 판정 불가로 전달됐다. Sandbox의 준비 단계 종료 코드와 실패 원본 정보는 함께 보존됐다. 이번 검증에서 Judge·모델·에이전트 루프를 수정하지 않았다.

7건 모두 실행 중 중복 요청 차단, 실제 큐 메시지 재전달 시 결과 재사용, PostgreSQL 최종 저장과 SQLite 재사용 이력을 확인했다. 7건의 유효 실행 시도는 8회다. 오래된 Backend 이미지로 처음 실행했다가 저장 검증에 실패한 1회까지 포함해, 총 9회 실행 자원의 namespace·이미지·요청 볼륨·작업 폴더가 남아 있지 않은 것을 독립 조회로 확인했다.

처음 사용한 기존 Backend Docker 이미지에서는 이미 소스에서 수정된 PostgreSQL 타입 오류가 발생했고 요청한 Chaos 관측도 결과에 없었다. 해당 실행은 성공 집계에서 제외하고 결과를 보존했다. 현재 소스를 빌드해 재실행한 위 7건에서 최종 저장을 확인했다. 배포 시 이미지 태그와 실제 실행 버전을 확인해야 한다.

멀티레포 부하는 목표 5명으로 요청했다. 5명 100회·10명 200회 업무 시도에서 실패 0건, 50명 오류율 2.44%, 100명 8.87%, 200명 79.12%를 기록했다. Container Kill 복구 2.62초·실패 1건, selector 단절 복구 10.95초·실패 15건, DB 장애 복구 16.77초·실패 6건을 기록했다. 목표와 고부하 단계의 경고를 함께 전달했다.

실제 CodeReferee 프론트의 입력 화면과 단일·멀티레포 완료 결과 조회도 확인했다. 별도 임시 브라우저 프로필에서 화면 JavaScript 오류는 없었다. 제출 버튼부터 새 요청을 생성하는 브라우저 클릭 흐름은 이번 검증에 포함하지 않았다.

추가 검증에서도 Git fixture URL을 프로세스별 로컬 Git mirror로 연결했다. LLM 호출·자동 패치 생성, AWS·Mac, 모든 Chaos 종류 재실행, 새 Prometheus 자원 메트릭 전송 실측은 이번 추가 검증 범위에 포함하지 않았다. 기존 회귀 테스트 결과는 앞 절의 기록을 따른다.

추가 원본은 로컬 `C:/Capstone_Project/tmp/final-e2e-20261112`에 보관한다.

## 검증 범위와 다음 작업

이번 범위는 설정 파일로 구성한 멀티레포의 Sandbox 내부 업무·대표 장애·부하·실패 정리와 전체 큐 연동 검증이다. 모든 Chaos 종류를 모든 구성에서 다시 실행한 것은 아니다. AWS·Mac 실측과 외부 공개 프로젝트의 멀티레포 구성은 후속 검증이다.

우선순위에 따라 다음 작업을 남긴다.

1. 선행 PR 병합 후 통합 기준 코드 재확인과 실제 AWS 배포 환경에서 멀티레포 요청 확인.
2. 추가 레포별 patchDiff 대상·기준 SHA·AI 소스 문맥 계약과 수정 후 그룹 재검증.
3. 전용 빌더·캐시 용량 상한·보존 기간과 빌드 컨텍스트 정리. 다른 프로젝트 캐시와 분리해 공간 회수 가능하게 구성.
4. 외부 프로젝트·여러 서비스 의존 관계·인증 및 외부 시스템 재현의 추가 사례 검증.
5. 레포별 입력 UI와 전체 SHA 조합을 사용하는 이력·재검사 식별 규칙.
6. 여러 Sandbox 배정·동시 실행 상한·EC2 자동 기동·종료.
7. Sandbox 실행 상태 조회가 Chaos 실행 중에도 마지막 배포 단계인 rollout에 머무르는 표시 개선. 이번에는 실제 Pod·이벤트로 진행을 확인했다.

원본 로그·응답·실행 드라이버는 로컬 `C:/Capstone_Project/tmp/multi-repository-20261111`에 보관한다. 요청 자료와 로컬 환경 파일은 커밋 대상에서 제외한다.
