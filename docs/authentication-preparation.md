# 인증 준비503 원인 분석과 프론트 지원 범위

2026-11-06, #34 기반의 별도 후속 진단이다. 원본 JAR를 다시 사용하고 gateway/identity/financial과 PostgreSQL을 요청 namespace에 배포했다.

## 재현에서 확인한 원인

회원가입은 identity의 회원 저장과 financial의 `/internal/deposits` 지갑 생성을 함께 호출한다. 앞선 대표 업무 경로 설명에 이 의존성을 보충한다.

- 원본 제한에서 gateway 첫 회원가입은503/GW-001, 약1.36초였다. identity 직접 회원가입도503/MB-010, 약1.60초였다.
- identity 로그에 `TimeoutException: TimeLimiter 'commerceWallet' recorded a timeout exception`을 확보했다. financial의 지갑 생성은201로 처리된 표본이 있었다.
- 공통 `InternalCircuitBreakerConfig`는3초를 명시하지만 지갑 전용 `WalletCircuitBreakerConfig`는 이름별 CircuitBreaker만 설정한다. 이 경로의 TimeLimiter 제한을 명시하는 설정이 필요했다. gateway도 HTTP route5초와 별도의 TimeLimiter 설정을 구분해야 한다.
- 같은 코드와 자원에서 gateway 인증 TimeLimiter5초, identity 지갑 호출3초를 명시한 뒤 첫 회원가입200/3.738초, 다음 회원가입200/0.320초, 직접 로그인200을 확인했다.

HTTP 요청은 별도 Python 클라이언트로도 수행했다. 따라서 재현된503은 실제 대상 앱 응답이다. 기존 기록에 있던 모든503의 응답 본문과 원인을 소급 확인한 결과와는 구분한다.

라이브러리는 TimeLimiter 기본값1초를 사용하고 설정 속성으로 제한을 명시할 수 있다. [Resilience4j 기본값](https://github.com/resilience4j/resilience4j/blob/master/resilience4j-timelimiter/src/main/java/io/github/resilience4j/timelimiter/TimeLimiterConfig.java), [Spring Cloud 설정](https://docs.spring.io/spring-cloud-circuitbreaker/reference/3.3/spring-cloud-circuitbreaker-resilience4j/circuit-breaker-properties-configuration.html)

## Sandbox에서 보완한 부분

준비 요청과 실제 측정 요청에 같은 timeout을 사용하던 것을 선택 설정으로 분리했다. 실제 첫 회원가입3.738초는 기존 observer 요청 제한2초를 넘는다.

```yaml
probeSpec:
  setupRequestTimeoutSeconds: 10
  setup:
    - method: POST
      path: /login
      body:
        email: 'sandbox-${runId}@example.test'
        password: reproduction-only
      extract:
        token: data.accessToken
  steps:
    - path: /profile
      headers:
        Authorization: 'Bearer ${token}'
```

- 준비 요청마다1~30초를 명시한다. 생략한 설정은 기존 timeout을 사용한다.
- 측정 steps의 요청 제한과 성공 조건은 별도로 적용한다. 준비 요청은 측정 표본에서 제외한다.
- observer와 부하 생성기가 같은 설정을 사용한다. 부하 준비 시간 예산에도 선언 값을 반영한다.
- 실제 HTTP 회귀에서 느린 로그인 준비는 통과하고, 같은 지연의 측정 요청은 짧은 원래 제한으로 실패하는 것을 확인했다.
- POST를 자동 재전송하는 기능은 추가하지 않았다. 재시도 횟수·멱등성은 별도 정책으로 합의한다.

## 대상 앱 재현 설정 예시

아래는 전체 validation.yaml 중 env 조각이다. 해당 앱의 의도한 제한을 확인한 뒤 명시하는 예시이며, 관측된 실패 기록도 함께 보존한다.

```yaml
services:
  gateway:
    env:
      SPRING_APPLICATION_JSON: '{"resilience4j":{"timelimiter":{"instances":{"identityAuth":{"timeoutDuration":"5s"},"identityApi":{"timeoutDuration":"5s"}}}}}'
  identity:
    env:
      SPRING_APPLICATION_JSON: '{"resilience4j":{"timelimiter":{"instances":{"commerceWallet":{"timeoutDuration":"3s"}}}}}'
```

## 실행 검증

원본 제한과 명시적 제한의 비교 JSON, 비밀값을 가린 앱 로그, Sandbox observer/부하 결과는 로컬 `tmp/auth-startup-20261106`에 보존한다. 준비503 원인 비교 후 새 identity에서 Sandbox 준비10초/측정2초를 적용해 실제 observer와 인증 부하를 실행했다.

추가 실행 결과는 다음과 같다.

- 새 identity에서 준비 제한10초·측정 제한2초로 실제 Sandbox observer의 회원가입·로그인·프로필 baseline을 통과했다.
- 인증 부하1/2 동시 요청이 `observed / exitCode0`, 실패 표본0으로 끝났다. API 결과에 선언한 준비 제한10초가 남는 것을 확인했다.
- 같은 설정의 실제 Container Kill이 `observed / exitCode0`으로 끝났다. 선택 Pod1개, 업무 실패 표본36개 후 복구98.21초를 관측했다. 이 시간은 해당 진단 환경의 표본이다.
- Windows 네이티브296개 중295개 통과, POSIX 권한1개 건너뜀. Linux 전체308개 통과이며 같은 권한 및 Bash 테스트를 포함한다.
- 현재 AI 파서로 추가 부하/Container Kill 결과와 준비 제한 출처가 유지되는 것을 확인했다.
- 진단 namespace, gateway/identity/financial 요청 이미지, 작업 폴더 정리를 확인했다. 초기 축소 구성의 실패도 따로 보존했다.

#34의 기존5앱12종/별도Redis 실측과 이번 추가 진단을 구분한다. 새 선택 설정의 실제 검증은 cold setup·부하·Container Kill이며 전체 시나리오 묶음 재실행은 추가 범위다.

## 진단 중 확인한 별도 조건

- 최초 축소 재현은 필수 URL 설정과 financial을 제외해 별도 기동/의존성 실패가 났다. 기록을 보존하고 필수 의존성을 포함한 뒤 비교했다.
- financial 스케줄러에서 `shedlock` 테이블 부재 오류를 확인했다. 이 스케줄러의 업무 검증에는 별도 DB 초기화가 필요하다. 대표 회원가입 검증 범위와 함께 기록한다.

## 현재 프론트 검증 범위

| 범위 | 현재 상태 |
| --- | --- |
| package.json의 의존성 설치·빌드·선언된 테스트 | Node 검사 경로로 실행 가능 |
| Dockerfile/명시적 설정으로 정적 파일을 서빙하는 앱 | 배포와 HTTP 응답·적용 가능한 서버 장애/부하 관측 가능 |
| 설정 없이 React/Vite URL 자동 실행 | 후속 작업 브랜치에서 Vite SPA 정적 빌드·서빙을 구현하고 실제 두 레포로 검증했다. [조건과 실행 범위](frontend-static-validation.md) 참고. |
| 버튼 클릭·로그인 UI·클라이언트 라우팅·브라우저 API/CORS 검증 | 별도 브라우저 E2E 후속 범위 |

인증 준비 진단 당시 프론트 계획은 `stack_detection`, port3000, `npm run dev`였다. 프론트의 실제 빌드·배포·장애 검증은 이후 별도 작업에서 수행했으며 위 링크에 기록한다. Vite의 개발 서버 설정은 [공식 서버 옵션](https://vite.dev/config/server-options.html)을 참고한다.
