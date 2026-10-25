# PR #17 보완 검증 (2026-10-12)

기존 구현은 `$PORT` 문자열의 존재만 확인해서 주석, 다른 옵션, PORT_SUFFIX,
PORT 재정의, loopback, single-quoted PORT, 중복 web 선언을 받아들였습니다.
추가한 7개 거부 테스트는 수정 전 모두 실패했고 수정 후 통과했습니다.

## 검증 결과

- Windows Python 회귀 테스트: 136개 실행, 성공, 1개 건너뜀.
- POSIX 테스트: Windows 직접 실행은 셸 차이로 실패하여 결과를 분리했습니다.
  WSL Ubuntu에서 Bash 부트스트랩 12개 테스트 모두 통과했습니다.
- Procfile Gunicorn/Flask fixture를 resolve_plan으로 탐색했습니다.
  생성 Dockerfile과 실제 빌드 입력의 동일성을 확인한 뒤 Docker Desktop Linux에서
  이미지 빌드·기동, HTTP 200과 JSON 응답을 확인했습니다.
- 해당 실행 전용 컨테이너와 이미지 태그는 제거했습니다. 공유 이미지나 빌드 캐시는 삭제하지 않았습니다.

이번 실제 실행은 선언 탐색 → 이미지 빌드 → HTTP 응답 검증입니다.
이 초기 검증은 Kubernetes 배포·Chaos·Backend/AI 전체 E2E 및 모든 지원 runner의
실행 검증은 아닙니다. 이후 수행한 Sandbox 내부 E2E는 아래에 따로 기록합니다.
공개 예제 레포의 오래된 의존성이 Python 3.12에서
동작한다는 보장도 하지 않습니다. 설정 탐색 성공과 실제 실행 성공은 구분합니다.

## 추가: Sandbox HTTP API 내부 E2E

Windows + Docker Desktop + 기존 kind-codereferee에서 보완 코드로 실행했습니다.
공개 테스트 레포의 healthy-fastapi 브랜치를 실제 clone하고 요청의 patchDiff로
app.py를 entrypoint.py로 변경한 뒤 Procfile을 추가했습니다. 루트의 관습적인
Python 엔트리 파일이 없어 Procfile 경로를 사용해야 하는 조건입니다.
validation.yaml, Dockerfile, 명시적 deploymentProfile은 사용하지 않았습니다.
변경은 요청별 clone에만 적용했고 테스트 원격 레포에는 push하지 않았습니다.

실제 HTTP POST /repositories/validate → clone → patch 검사/적용 → Python 의존성 설치·compile
→ Procfile 탐색 → 이미지 build → kind load → Deployment/Service 배포
→ in-cluster Service HTTP 관측 → 결과 반환과 정리를 확인했습니다.

| 요청 | 결과 |
|---|---|
| 21238379-00f0-447c-8c0e-09f65a995db7, 일반 검사 | observed, exitCode 0, HTTP 200 |
| 4dad47ac-353c-4174-8150-4ff1280b5dd6, Container Kill | observed, exitCode 0, recovered true, HTTP 복구 2.61초 |

Container Kill은 Litmus Pass/Completed, 컨테이너 재시작 0→1과 container ID 변경을 확인했습니다.
복구 2.61초는 HTTP 표본에서 관측한 중단 구간이며 실험 전체 소요 시간과 다릅니다.
두 건 모두 namespace 제거, host image 제거, kind node image 제거, workspace 제거를 확인했습니다.
별도 kubectl 조회로 namespace가 남지 않았음도 확인했습니다. 원본 JSON과 API 로그는
로컬 .runtime/evidence/procfile-pr17-e2e에 보존하며 커밋하지 않습니다.

### 실제 실행 중 발견·수정한 회귀

첫 요청 0553e814-31ca-4087-a768-0e9b26720703은 Python 설치 중 /cache/python/lib
Permission denied로 실패했습니다. 이 실행은 성공으로 세지 않았습니다.
Windows의 비-Node 검사는 기본 uid 0인데 캐시를 uid 1000으로 만들어,
cap-drop ALL 환경에서 쓰기가 불가능했습니다. 캐시 소유자를 실제 검사 사용자와
맞추도록 수정했습니다. Linux 호스트 uid 동작과 Windows Node uid 1000 동작은 유지합니다.
제출 코드에 capability를 추가하거나 공유 캐시 권한을 확대하지 않았습니다.

최종 코드 Windows 회귀: 137개 실행 성공, POSIX 전용 1개 건너뜀.
WSL Ubuntu repository_checks 테스트 13개 성공(건너뛴 권한 테스트 포함).
이전 WSL Bash 12개 통과 기록도 유지합니다.

이번 추가 검증은 Sandbox 내부 E2E입니다. Backend/Redis/AI, LLM 패치 생성,
브라우저 클릭, AWS, 외부 Prometheus 자원 지표의 전체 E2E는 재실행하지 않았습니다.
Mac에서 이번 캐시 보완을 직접 실측한 것도 아닙니다.

## 추가 리뷰: 의존성 설치 선언 누락 거부

requirements.txt가 없으면 생성 이미지가 실행기를 설치하지 않는 누락을 수정했습니다.
Procfile 자동 배포는 requirements.txt를 요구하며, 다른 설치 방식은 Dockerfile과
validation.yaml로 명시하도록 ConfigurationRequired를 반환합니다.
pyproject.toml만 있는 경우도 이 자동 생성 경로에서는 지원한다고 간주하지 않습니다.

Windows 최종 회귀 테스트: 139개 실행 성공, POSIX 전용 1개 건너뜀.
이 변경은 정상 의존성 설치 경로를 변경하지 않고 누락된 선언을 거부하는 조건입니다.
위 일반 검사·Container Kill 성공 E2E는 직전 코드의 기록이며 이번 조건 추가 후
전체 Chaos를 재실행한 기록으로 해석하지 않습니다.

실제 Sandbox HTTP API에 requirements.txt를 제거한 Procfile 테스트 레포를 요청했습니다.
요청 ID: 6efb8b40-16e3-4937-bbd2-6b5c8a438a14.
configuration_required=true, failed_step=detect, exitCode=89 및 설치 설정 안내를 확인했습니다.
이 응답은 기존 ConfigurationRequired 결과 계약을 유지하며 HTTP 4xx나 infra ERROR로
변경한 것은 아닙니다. 배포·HTTP 서비스 검사는 시도하지 않았습니다.
별도 조회로 요청 namespace·이미지가 없고 clone 작업 폴더가 제거됐음을 확인했습니다.
원본 JSON은 로컬 evidence에만 저장합니다.

## 최종 경계 조건 재검토 및 E2E (2026-10-12)

추가 검토에서 다음 선언이 기존 검사에 통과함을 재현했고 설정 요구로 보완했습니다.

- TCP 포트 선언과 함께 Unix socket 또는 fd 실행을 지정하는 명령.
- daemon, 설정 검사/출력 후 종료, TLS 등 foreground plain HTTP와 다른 실행 모드.
- 생성 이미지에서 준비하지 않는 .venv/bin 등의 실행기 경로.
- console script 이름을 python -m 모듈로 간주하는 경우.
- 셸 주석/리다이렉션/환경 대입과 PORT 바인딩 외 변수 확장.
  특히 $PORT#note는 실제 Bash에서 8000#note로 전달되지만 이전 shlex 주석 처리로는
  $PORT로 잘못 판단됐습니다.
- 생성 이미지의 PORT와 validation.yaml env.PORT가 충돌하는 경우.

정상적인 직접 실행기, 지원 Python 모듈, 사용자 지정 포트, 같은 PORT 값,
명시적 Dockerfile/설정 우선순위도 회귀 테스트로 확인했습니다.
모든 runner의 모든 옵션을 해석하는 shell parser는 아니며, 모호한 명령은
명시적인 Dockerfile/validation.yaml로 배포해야 합니다.

### 최종 테스트

- Windows: 145개 실행 성공, POSIX 전용 1개 건너뜀.
- Docker Linux Python 3.12 + requirements.txt 고정 의존성: 전체 157개 성공, 건너뜀 없음.
  Docker 소켓을 마운트하지 않고 소스는 readonly, 테스트 실행 폴더만 전용 tmpfs로 제공했습니다.
- 환경 준비 중 WSL에는 FastAPI와 ensurepip가 없어 전체 실행이 실패했습니다.
  첫 Linux 컨테이너 실행도 .runtime 쓰기 공간이 없어 테스트 1개가 실패했습니다.
  이 실행들은 성공으로 세지 않았고, 실행 환경을 갖춘 최종 전체 실행 결과가 위 157개입니다.

### 최종 코드 실제 Sandbox API E2E

| 요청 | 결과 |
|---|---|
| c0f6543f-ff29-4df1-9dae-fef6b6f74f7a | Procfile 기반 일반 검사: HTTP 200, observed, exitCode 0 |
| b9e361d3-edc7-4dc4-ba58-82f02ea830d9 | Container Kill: Litmus Pass/Completed, 실제 재시작 0→1, recovered true, HTTP 복구 2.57초, exitCode 0 |
| 6936cb6f-d8eb-4a0d-ab93-2254e2a218ba | requirements.txt 누락: configuration_required=true, detect 단계 exitCode 89, 배포 미실행 |

정상 두 요청은 actual clone → 요청별 patch → 의존성 검사 → Procfile 기반 이미지 생성
→ kind 배포 → Service 경유 관측 → 결과 반환을 실행했습니다.
요청 namespace·호스트/kind 이미지·workspace가 정리됐고 cleanup errors는 없습니다.
거부 요청은 namespace와 요청 이미지가 없고 workspace가 제거됨을 별도 조회했습니다.
실측 입력은 이전처럼 비관습적인 entrypoint.py와 Procfile을 사용했습니다.
원본 JSON은 로컬 .runtime/evidence/procfile-pr17-e2e에 보존합니다.

이 기록은 최종 Procfile 처리 코드의 로컬 Sandbox 내부 검증입니다.
AWS, Backend/AI 전체 연동, LLM patch 생성, 모든 runner의 실제 서비스 실행,
모든 공개 레포의 배포 성공을 보장하는 기록으로 확대하지 않습니다.
