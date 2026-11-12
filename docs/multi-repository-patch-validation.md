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

## 남은 범위

외부 LLM 실제 생성, 공개 GitHub 멀티레포 프로젝트, Linux/Mac·AWS의 이 기능 실측은 별도다.
레포별 패치 UI와 여러 레포의 협조적 동시 수정, 자동 서비스 탐색·EC2 온디맨드는 이번 완료 범위가 아니다.
병합 후 최종 버전의 전체 큐·배포 검증과 메트릭 연결 확인은 배포 체크리스트에서 진행한다.

원본 응답·검증 스크립트·실패 로그는 Git 추적 밖 `C:/Capstone_Project/tmp/multirepo-patch-20261113`에 보존한다.
