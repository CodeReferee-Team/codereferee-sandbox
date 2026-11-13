# 멀티레포 순차 패치 검증 기록

2026-11-13 Windows·Docker Desktop·기존 kind-codereferee에서 실행했다.
[요청 계약](multi-repository-patches.md)과 실제 생성 모델의 수리 성능을 구분한다.

## 실제 실행 경로

각각 커밋한 Python gateway와 API 레포를 로컬 Git mirror로 준비했다.
프로세스별 Git URL 재작성만 사용했으며 실제 clone·SHA checkout·Docker 검사/빌드·kind 이미지 적재·그룹 배포를 수행했다.
요청 전용 Redis를 연결하고 Litmus 네트워크 단절로 Redis 장애를 주입한 뒤 기존 부하 생성기를 실행했다.
가상 사용자 목표는 5명, 부하 단계는 5·10·50·100·200명 각 20초다. 제품의 장애·복구 제한은 늘리지 않았다.

AI의 실제 LangGraph 재검증 루프, 기존 Refiner 편집/패치 가드, HTTP Sandbox 파서와 Judge를 사용했다.
**LLM 응답 생성만 고정된 편집 대역을 사용했다.** Critic은 대역으로 통과시켰다. 실제 모델의 원인 분석·패치 생성 성공으로 집계하지 않는다.
원본 관측 후 편집 대역의 함수 인자 오류를 발견해 수정했고, SHA가 일치하는 저장된 원본 실측에서 루프를 이어 실행했다.
처음부터 중단 없이 실행된 Backend 큐 E2E라고 주장하지 않는다.

| 단계 | 실제 결과 | Judge 및 패치 |
| --- | --- | --- |
| 원본 그룹 | Redis 복원 뒤에도 API가 503을 유지, observed / exitCode 1 / recovered false | Fail, backend 소유 app.py 문맥 선택 |
| 1회차: backend 코드 패치 | Redis 장애 복구 성공. 부하 목표 5명에서 100회 중 59회 실패 | Fail 유지, 기준 레포 main의 설정 문맥으로 전환 |
| 2회차: main 설정 패치 | backend 패치를 유지해 그룹 전체 재배포. Redis 복구, 목표 5명 100회 중 실패 0회 | Pass, patch_verified true |

수정 후에도 고부하 전체가 성공한 것은 아니다. 200명에서 1,633회 중 시간 초과 201회를 기록했다.
목표 5명 충족과 고부하 경고를 구분한다. 제출 원본 판정과 실행 결과는 그대로 Fail로 복원됐다.
실제 두 회차에서 레포별 패치 SHA256·기준 SHA·적용 여부가 결과에 남았다.

## 브랜치 이동과 누적

관측 후 두 fixture 브랜치에 새 커밋을 추가했다. 다음 그룹 배포·문맥 수집은 최초 SHA를 유지했다.
main은 cb155c33693ddc9c963f1a582527854cad264d72, backend는 a529404431471abf33219ded44df28191a4b17dc다.
main 패치를 추가해도 backend 패치는 없어지지 않았고, 실제 마지막 배포에 두 패치 모두 applied true로 기록됐다.
추가 레포 간 전환과 전체 3회 상한은 그래프 테스트로도 확인했다. 실제 라이브 전환 사례는 backend → main이다.

## 실패·재사용·정리

- 미선언 alias: 실제 API/Git 요청에서 exitCode 89, 이미지 빌드 전에 거절.
- 패치 SHA와 repositoryCommits 불일치: exitCode 89, 추가 clone 전에 거절.
- CI 보호 경로: exitCode 1, patch 단계 거절.
- 위 세 거절 응답 모두 정리 완료. 동일 요청 재전송은 저장 결과를 재사용하고 resource ID를 늘리지 않았다.
- 실패 기록을 포함한 resource ID 8개를 별도 Docker·Kubernetes 조회로 대조했다.
  namespace·관측 namespace·요청 컨테이너·볼륨·호스트/kind 이미지·작업 폴더가 남지 않았다.
- 검증 API 프로세스는 종료했고 기존 클러스터·공유 receiver·다른 프로젝트 컨테이너는 유지했다.

첫 fixture는 재현 Redis에 불필요한 인증을 시도해 baseline에서 중단됐다. fixture만 수정하고 실패를 보존했다.
미복구 진단에서 `scripts.pod_diagnostics`의 API 패키지 import 오류도 실제 재현했다.
해당 오류는 별도 fix 커밋으로 수정하고 독립 Python 인터프리터 import 회귀를 추가했다.

## 회귀 및 출력 계약

- Sandbox Windows: 372개 중 371개 통과·플랫폼 조건 1개 제외. POSIX Bash 12개는 이번 Windows 목록에서 제외했다.
- AI 신규 라우팅: 19개 통과. 레포 전환·누적·SHA·전체 3회 상한·재검증 heartbeat 포함.
- AI 관련 회귀: 162개 중 158개 통과, Windows SQLite 임시 파일 잠금 오류 4개.
  분리한 변경 전 HEAD 5d28d40에서도 같은 4개가 재현됐다. 전체 통과로 쓰지 않는다.
- 실측 패치/회차를 기존 결과 이벤트로 직렬화하고 SQLite에 저장·조회해 두 레포 패치와 2회차 기록 보존을 확인했다.
  새 Backend → Redis → AI → Sandbox → Backend → PostgreSQL E2E를 재실행한 것은 아니다.

## 실제 로컬 LLM E2E 후속 결과

Ollama `exaone3.5:2.4b`와 실제 Backend·Redis·Worker·Sandbox·kind를 연결해 두 번 실행했다.
GitHub 형식 URL은 프로세스 한정 설정으로 커밋된 로컬 Git fixture에 연결했다.
모델 응답을 대체하지 않았다. 두 번째 요청은
`e75064d5-999b-4c0f-ab43-d7755fbe3da5`다.

원본 Redis 장애 미복구와 메트릭 수신, 실제 LLM 응답, Backend FAILED 및 PostgreSQL/SQLite
보고서 저장을 확인했다. 로컬 모델이 유효한 편집 스키마를 만들지 못해 패치 재배포는 실행되지 않았다.
`patch_verified=false`이며 실제 LLM 자동 수리 성공은 미검증이다.
저장된 두 레포 누적 패치의 앞선 실측과 이번 모델 생성 실패를 각각 보존한다.

API 모델 입력의 하위 장애 종류 누락과 Backend의 수정 회차 저장 누락을 별도 브랜치에서 보완했다.
Backend 전체 테스트 64개, AI 라우팅·Refiner 관련 테스트 56개가 통과했다.
별도 Redis 재생 요청 `22263baf-b3ca-431d-9e83-7fcc87be3f39`에서는 앞서 실측한
backend→main 두 회차와 패치 보고서가 Backend 조회·PostgreSQL·재기동 후에도 유지됐다.
이는 저장된 관측의 계약 재생이며 실제 LLM 자동 수리 성공으로 집계하지 않는다.
후속 두 요청을 포함한 resource ID 10개를 독립 조회해 요청 namespace·컨테이너·볼륨·
호스트/kind 이미지·작업 폴더가 남지 않음을 확인했다.

## 남은 범위

유효한 실제 LLM 패치 생성·재배포, 공개 GitHub 멀티레포 프로젝트, Linux/Mac·AWS의 이 기능 실측은 별도다.
레포별 패치 UI와 여러 레포의 협조적 동시 수정, 자동 서비스 탐색·EC2 온디맨드는 이번 완료 범위가 아니다.
병합 후 최종 버전의 전체 큐·배포 검증과 메트릭 연결 확인은 배포 체크리스트에서 진행한다.

원본 응답·검증 스크립트·실패 로그는 Git 추적 밖 `C:/Capstone_Project/tmp/multirepo-patch-20261113`에 보존한다.

## 기존 검증 모델의 실제 재배포 E2E 후속

Llama 3.1 8B와 Qwen 2.5 Coder 7B로 실제 모델 편집 생성과 재배포를 실행했다.
최종 Qwen 요청 `2d1da9c8-2b96-4b59-b9f9-a7940a1447ff`는 두 로컬 Git 레포의
재연결 대기값 제어 사례에서 모델 호출 8회와 패치 재배포 3회를 수행했다.
1·3회차에는 수정본을 배포한 뒤 실제 Redis 장애를 다시 주입했고 복구되지 않았다.
2회차는 수정본의 Python 컴파일 오류로 중단됐다.
Backend·PostgreSQL·SQLite 보고서와 세 회차가 일치했고 메트릭 수신 상태는 `received`였다.
수리 결과는 `patch_verified=false`로 보존했다.

AI 연동에서는 실제 실패 단계 로그 전달, 선택 레포 URL 유지, Windows의 누적 패치
stdin UTF-8 전송을 보완했다. Critic 안내 세 줄은 실험 후 최종 연동 범위에서 제외했다.
Critic 실험 안내를 제거한 최종 코드에서 관련 AI 회귀 120개가 통과했다. Judge 판정은 기존 규칙으로 계산했다.

현재 원격 AI 코드는 모델을 환경변수로 선택한다. 제공된 팀 대화에서 운영은 Groq의
`openai/gpt-oss-120b`로 결정됐음을 확인했다. 과거 로컬 Llama 시험과 실제 운영 API를 구분한다.
AI의 `docs/multi-repository-patches.md`에 각 요청과 원격 설정 확인 결과를 기록했다.

### 양성 제어와 자원 복원

같은 제어 사례에 사람이 작성한 재연결 대기값 1초 패치를 적용한 별도 실행은
실제 Redis 장애 후 14.91초에 복구했다. `observed / exitCode 0 / recovered true`와
요청 정리 완료를 확인했다. LLM 성공률이나 Backend E2E 결과에는 합산하지 않는다.

실패를 포함한 resource ID 26개의 요청 namespace·컨테이너·볼륨·호스트/kind 이미지·
작업 폴더 잔존이 없음을 독립 대조했다. 기존 Docker Desktop Litmus 워크로드 10개도
저장한 원래 복제본 수로 복원했고 전체 Ready를 확인했다.
검증용 신규 Ollama 가중치를 정리한 뒤 C 드라이브 여유는 약 6.38GB였다.

## Sandbox 멀티레포 계약 최종 재검증

모델 성능 평가에서 범위를 좁혀, 알려진 정상 패치를 main과 backend에 함께 전달한 실제
Sandbox API 실행을 추가했다. 두 레포의 고정 commit SHA, 패치 SHA-256, 적용 완료를 응답에서
대조했다. 실제 Redis 장애와 부하 시나리오가 모두 실행되고 복구됐으며 미실행 항목은 없었다.
목표 동시 사용자 5명 구간은 100요청 중 실패 0건이고 `target_met=true`였다.
200명 구간에는 시간 초과 57건이 관측됐으므로 전체 부하 단계 무오류로 해석하지 않는다.

응답은 `observed / exitCode 0 / recovered true`였다. 같은 요청 재전송은 저장된 결과를
반환했고 새 실행을 만들지 않았다. Sandbox 관련 회귀 25개가 통과했으며, 이번 실행을 포함한
resource ID 27개의 namespace·컨테이너·볼륨·호스트/kind 이미지·작업 폴더 잔존이 없었다.
원본은 로컬 검증 경로의 `sandbox-focused-result.json`과 `sandbox-focused-summary.json`이다.

이 실행은 정상 패치를 이용한 Sandbox 계약 검증이다. 앞서 실제 Llama가 생성한 패치를
재배포한 기록은 별도로 보존하며, 그 패치의 자동 수리 성공은 확보하지 못했다.

### 최종 로컬 Llama 전체 큐 E2E

최종 연동 코드로 `llama3.1:8b`를 다시 실행했다. 요청은
`b0d32d78-f48e-45a1-a177-23b3e6105fe7`이며 실제 Backend 제출 → Redis → Worker →
Sandbox 원본 Chaos 관측 → 실제 LLM 호출 → 패치 재배포 → Backend 저장을 수행했다.
새 코드·모델 응답·패치를 대역으로 교체하지 않았다. Git URL은 커밋된 로컬 fixture 두 개에 연결했다.

모델 호출 9회, backend 레포 패치 재배포 2회가 실행됐다. 두 수정본은 Python 들여쓰기 오류로
build 단계에서 실패했다. 마지막 생성안은 선택된 backend 레포에 없는 설정 파일을 수정하려 해
기존 편집 가드와 재요청 후 거절됐다. main으로 전환해 수리하는 성공 사례는 확보하지 못했다.
Backend는 `FAILED`, `patch_verified=false`로 종료됐고 두 회차와 최종 보고서의
PostgreSQL·SQLite 일치, 원본 메트릭 `received`를 확인했다.

이 결과는 실제 LLM을 포함한 전체 큐 경로의 실패 처리 검증이다. 순차 멀티레포 자동 수리 성공을
확인하려면 적절한 패치를 생성하는 모델 설정으로 추가 실행이 필요하다.
원본은 로컬 검증 경로의 `llm-e2e-llama-final`에 보존했다.
독립 자원 대조에서 이번 실행을 포함한 resource ID 30개의 Sandbox 자원 잔존이 없었다.
