# 범용 레포 실행 실측 (2026-10-07)

kind 클러스터와 실제 `POST /repositories/validate` 경로를 사용했다.
AI Judge 정책은 수정하지 않았고 아래 성공은 합격 라벨이 아니라 Sandbox 실행·관측 성공이다.

| 레포 | 실행 설정 | 실제 결과 |
| --- | --- | --- |
| docker/getting-started-app (`6b025fc53bc7`) | URL 자동 탐색, 생성 Node Dockerfile | HTTP smoke 및 Container Kill·복구 성공 |
| dockersamples/python-flask-redis (`3b11b7ac8c53`) | URL 자동 탐색 | Compose 8000과 EXPOSE 5000 충돌로 설정 필요 반환 |
| 위 Python + Redis 레포 | patchDiff로 validation.yaml 보완 | Container Kill·복구 성공, 복구 관측 2.44초 |
| 2025-hajithon/2025-team04-whik-server (`59b88a2f2a27`) | URL 자동 탐색 | Compose에 build 대상이 없어서 설정 필요 반환 |
| 위 Whik 레포 | patchDiff로 재현 Dockerfile·YAML·MySQL 설정 | Container Kill·복구 성공, 복구 관측 69.75초 |

세 Chaos 성공 요청은 `observed`, `exitCode: 0`, `recovered: true`였고
namespace·호스트 이미지·kind 내부 이미지 정리가 성공했다. 실행마다 다른 결과가 나올 수 있으므로
위 복구 시간은 측정 사례이지 서비스의 고정 성능이나 합격 기준이 아니다.

Whik 최초 재현 시 Windows clone이 원격 LF를 CRLF로 바꾸어 `gradlew`가 Linux 빌드에서 실패했다.
clone별 `core.autocrlf=false` 적용 후 재검증했다. 이 실패도 `observed` + `failed_step: build`로
반환되어 인프라 오류와 구분되는 경로를 확인했다.

## Whik 재현에서 명시한 내용

- 원격 레포에는 로컬 JAR를 COPY하는 Dockerfile이 있어 clone 내부에서 Gradle `bootJar`를 수행하는
  별도 재현 Dockerfile을 patch로 추가했다. 사용자 원격 코드는 변경하지 않았다.
- 요청 전용 MySQL 8.4와 새 계정·비밀번호를 생성하고 `DATABASE_*` 환경 변수로 앱에 연결했다.
- `dev` 프로필 및 빈 재현 DB의 스키마 생성 설정을 명시했다.
- `/categories`를 HTTP probe로 사용했다. 원래 앱의 루트 `/`를 정상 API로 가정하지 않았다.
- MySQL은 운영 DB가 아니며 기존 데이터 복제·업무 데이터 seed는 하지 않았다.

이 과정은 **명시적 설정 경로**의 검증이지 Whik URL-only 자동 재현 성공이 아니다.
이 레포만을 위한 조건 분기를 Sandbox 코드에 추가하지 않았다.

## 재현 자료와 남은 범위

로컬 원본 결과는 `.runtime/evidence/auto-node-001.json`, `auto-node-chaos-002.json`,
`auto-python-conflict-003.json`, `auto-python-redis-004.json`, `whik-auto-005.json`,
`whik-mysql-006.json`, `whik-mysql-007.json`에 보존했다. 임시 patch는 `.runtime/`에 있으며
로그·원본 결과·실험용 clone은 커밋하지 않는다.

- 일반 HTTP smoke는 단위 테스트 실행이 아니다. 이후 build/test 연결 단계에서는 실제 명령과
  테스트 상태를 별도로 기록한다(아래 추가 검증).
- 이번 단계는 Sandbox 실제 API 검증이다. 최신 Backend·Redis·AI 전체 경로 재검증은 별도 단계다.
- 임의 멀티모듈, 여러 서비스의 Compose, 멀티레포 MSA 그룹, 전용 온디맨드 Worker는 아직 미완료다.
- 외부 `.env`, 운영 비밀번호·외부 인증을 자동 사용하지 않는다. 모호한 실행 계약은 설정 보완을 요청한다.
- 메트릭 remote-write 수명주기 연결은 별도 브랜치 작업이며 이번 실행 검증 완료에 포함하지 않는다.

## build/test 연결과 멀티모듈 추가 검증

- `checks-failure-008`: 실제 clone한 Python 레포에 명시적 검증 실패 명령을 patch로 전달했다.
  install·compile 후 `exitCode: 7`, `observed`, `failed_step: test`로 중단되어 배포·Chaos를 하지 않았다.
- `checks-cleanup-012`: 동일 실패를 요청 전용 Linux 캐시와 clone 정리 보강 후 다시 확인했다.
  캐시 볼륨·임시 clone이 모두 실제 삭제됐고 테스트 실패를 인프라 오류로 바꾸지 않았다.
- `whik-checks-009`: Windows bind mount 캐시 경로에서 600초 제한에 걸렸다. 성공으로 처리하지
  않았으며 검증 컨테이너 삭제를 확인했다. 이 결과를 학습용 서비스 실패 라벨로 사용하면 안 된다.
- `checks-native-cache-010`: 요청 전용 Docker 캐시 볼륨에서 실제 build 및 JUnit 테스트 1개가
  성공했다. 앱 배포·Container Kill·복구까지 성공했고 캐시·이미지·namespace를 삭제했다.
  전체 API는 726.792초였다. 기존 AI HTTP timeout이 600초라면 이 경우는 그대로 연동할 수 없으며,
  이미 빌드한 산출물을 재사용하는 경로 또는 요청 처리 방식·대기 계약의 보완이 필요하다.
- `multi-module-013`: `spring-guides/gs-multi-module`의 `complete` 프로젝트에서 application과
  library 테스트 2개를 실제 실행했다. `:application:bootJar`가 만든 JAR를 `artifactPath`로 선택해
  재컴파일 없는 런타임 이미지를 생성했다. `/actuator/health` 관측, Container Kill·복구,
  clone·캐시·호스트/kind 이미지·namespace 정리가 모두 성공했다.

원본은 `.runtime/evidence/`의 위 요청 ID JSON에 보존한다(내부 CLI로 실행한 cleanup-012는
실행 결과 출력으로 확인). 이는 단일 실행 서비스의 멀티모듈 검증이며 여러 서비스나 멀티레포 MSA
배포의 완료를 뜻하지 않는다. 최종 회귀 테스트는 64개 통과했다.
