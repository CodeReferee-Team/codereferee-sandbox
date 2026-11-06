# 인증 업무 경로가 있는 단일 레포 멀티서비스

`validation.yaml` v2는 실행 앱 **2~5개**를 명시한다. 자동으로 5개 서비스를 추측하거나 여러 레포를 묶는 기능은 아니다. 요청 하나는 기존처럼 직렬로 실행한다.

[실측 결과와 제외 범위](authenticated-multi-service-validation-results.md) · [후속 과제](multi-service-backlog.md)

## 루트 빌드·테스트와 검사 DB

루트 Gradle/Maven에서 여러 앱을 함께 빌드하는 경우 최상위 `verification`을 선언하면 레포 전체 검증을 한 번 실행한다. 선언하지 않으면 기존 서비스별 `verificationDirectory` 검증을 유지한다. 공통 검증의 테스트 수를 서비스 수만큼 복제하지 않는다.

```yaml
version: 2
verification:
  stack: gradle
  build: [sh, ./gradlew, bootJar, --no-daemon, --no-parallel]
  test: [sh, ./gradlew, test, --no-daemon, --no-parallel]
  env:
    DB_URL: jdbc:postgresql://localhost:5432/sandbox_test
    DB_USERNAME: sandbox
    DB_PASSWORD: reproduction-only
  databases:
    postgres:
      username: sandbox
      password: reproduction-only
      database: sandbox_test
```

위 조각에 아래 서비스 선언을 함께 작성한다. DB 환경 변수 이름은 앱이 실제로 읽는 이름을 사용한다.

- 검사 DB는 새 Docker 컨테이너에 생성한다. 호스트 포트를 게시하지 않고 검사 컨테이너와 전용 네트워크 공간을 공유한다.
- `verification.databases`는 postgres/mysql/redis/mongo를 종류별 하나씩 지원한다. 임의 host/port/image 지정은 허용하지 않는다.
- PostgreSQL 확장이 필요한 테스트는 `verification.databases.postgres.extensions: [vector]`로 고정된 pgvector 이미지를 선택한다. 배포 DB의 extensions와 별도이므로 양쪽 요구를 명시한다.
- 선언한 재현용 값만 사용한다. 운영 DB 주소나 실제 비밀번호를 넣지 않는다.
- 검사 DB 생성 실패는 인프라 오류이며 테스트 성공으로 처리하지 않는다. 성공·실패·timeout 후 검사 DB와 캐시를 회수한다.
- 이 설정은 Kubernetes 배포용 `dependencies`와 별개다. 테스트 DB와 서비스 실행 DB를 구분한다.
- 제품 기본 검사 timeout 600초는 변경하지 않았다. 장시간 실측의 명시적 timeout 인자는 로컬 검증 드라이버 범위로 기록한다.

## 서비스와 업무 probe

```yaml
entryService: edge
targetService: auth
probePath: /
services:
  auth:
    buildContext: .
    dockerfile: auth/Dockerfile
    port: 8081
    healthPath: /health
    env:
      DB_HOST: '{{dependency.db.host}}'
      DB_PASSWORD: '{{dependency.db.password}}'
  edge:
    buildContext: .
    dockerfile: edge/Dockerfile
    port: 8080
    healthPath: /health
    dependsOn: [auth]
    env:
      AUTH_URL: '{{service.auth.url}}'
dependencies:
  db:
    type: postgres
probeSpec:
  setupRequestTimeoutSeconds: 10
  setup:
    - method: POST
      path: /signup
      body:
        email: 'sandbox-${runId}@example.test'
        password: reproduction-only
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
      expect:
        success: true
```

경로·본문·JSON 응답 경로는 예시이며 해당 앱의 실제 계약으로 바꾼다. `expect`는 선택 사항이다. 이 예시는 edge를 통해 auth와 DB를 읽는 업무 경로이고, 장애 대상은 auth다. edge 자체 health만 확인하는 검사와 다르다.

- `setupRequestTimeoutSeconds`는 준비 단계 HTTP 요청마다 적용하는 선택 제한이다. 1~30초를 명시할 수 있고 생략하면 기존 요청 제한을 사용한다. 측정 steps의 timeout과 별도로 적용하며 결과의 `declared_setup_request_timeout_seconds`에 선언 값을 남긴다. 전체 observer 준비·실험 단계의 상한도 함께 적용한다.
- `setup`은 관측 Pod/부하 생성 Pod 시작 시 한 번 실행하며 측정 표본에서 제외한다. `${runId}`는 실행마다 다른 값이다.
- `steps`의 요청이 모두 HTTP 2xx이고 선택적 JSON `expect`가 일치해야 업무 성공이다. 401/404/429/5xx, redirect, JSON 추출·검증 오류와 timeout은 업무 성공이 아니다.
- 토큰은 응답에서 `extract`해 메모리에 보관한다. 결과 JSON에 응답 본문·토큰·probe 선언의 자격증명을 보내지 않는다.
- 부하의 `measurement_unit: workflow_attempt`는 전체 steps 한 번을 표본 하나로 센다는 뜻이다. setup은 제외한다. HTTP 응답을 받았지만 401/JSON 검증으로 거부한 표본도 응답 지연에 포함하고, 전송/프로토콜 오류는 지연 백분위에서 제외한다.
- 부하 생성은 한 재현용 인증 세션으로 동시 요청을 보낸다. 실제 사용자마다 별도 로그인하는 부하는 아니다. 각 측정 요청의 추출 상태는 분리한다.
- 지정한 Service의 상대 경로만 지원하고 redirect를 따라가지 않는다. setup/steps는 각각 최대 8개, 선언 전체는 최대 16 KiB다.
- setup 실패 시 실험을 시작하지 않는다. 정상 업무 baseline을 확보한 뒤에만 장애·부하를 실행한다.
- 관측 Pod가 setup 실패로 종료되거나 이미지를 받을 수 없으면 준비 대기를 조기에 중단한다. 단계 번호·HTTP 상태·안전한 오류 종류와 컨테이너 상태만 진단하며 응답 본문·인증 값은 포함하지 않는다.
- 기존 probeSpec 없는 GET probe 동작은 유지한다. 일반 연결 확인의 4xx 응답 허용 정책을 인증 업무 성공 정책으로 사용하지 않는다.

DB 장애 시나리오에도 동일 업무 흐름을 사용하려면 해당 `dependencies.db.probeSpec`을 명시한다. 이 경로의 기존 다단계 probe도 같은 HTTP 실행기를 사용한다.

## PostgreSQL vector 확장

```yaml
dependencies:
  db:
    type: postgres
    extensions: [vector]
```

명시한 경우에만 고정된 `pgvector/pgvector:0.8.7-pg17` 이미지를 사용한다. 일반 postgres의 이미지는 바뀌지 않는다. 확장 활성화/테이블 생성은 앱의 초기화 계약에 따른다. 임의 이미지나 SQL을 Sandbox 설정에서 실행하는 기능은 아니다.

## 검증 시 구분할 것

5개 앱의 배포 성공은 모든 업무 기능 검증 성공을 뜻하지 않는다. 실행한 probe 경로, 장애 대상, 외부 결제·스토리지·AI 모델 호출 등의 제외 범위, 자원 설정과 부하 단계를 실측 결과에 기록한다. 실패한 테스트를 건너뛰거나 health 성공을 전체 업무 성공으로 대체하지 않는다.

실측 드라이버에서 초기 계정 준비를 별도로 다시 시도한 경우 각 실패·재시도 횟수와 최종 baseline을 모두 남긴다. 이는 제품의 기본 자동 재시도 기능이나 장애 후 복구 실패를 재검증하는 정책을 뜻하지 않는다. 이 경우 초기 첫 요청의 무오류·cold-start SLO를 입증했다고 표현하지 않는다.

의존성에 별도 `probePath`/`probeSpec`을 지정하면 그 선언을 우선한다. 상위 시나리오의 인증 workflow가 이를 덮지 않는다. 상위 workflow와 같은 DB/Redis 업무를 관측하려면 해당 의존성에도 probeSpec을 명시한다.

라우팅 단절 등의 `recovery_seconds`는 실패 요청이 처음 관측된 뒤 다시 성공하기까지의 시간이다. 실패 표본이 없으면 0이며 무중단을 보장하지 않는다. `experiment_duration_seconds`는 주입·대기·정리를 포함한 별도 소요시간이다. Service endpoint를 없애도 기존 upstream 연결 등으로 업무 실패가 포착되지 않을 수 있으므로, 주입 성공과 업무 영향 관측을 구분한다.

## 후속 과제와 PR에 명시할 범위

- 제품 기능은 레포 독립적인 선언 계약으로 구현한다. 특정 레포 이름에 따른 분기, 고정 API 경로, 해당 앱의 결제/추천 로직을 Sandbox 코드에 넣지 않는다. 실제 레포의 경로·환경 변수·초기 데이터는 실측용 설정이며 제품의 공통 기본값으로 승격하지 않는다.
- 이번 단계의 목표는 앱 5개 배포와 인증된 대표 업무 경로의 장애·부하 실행 지원이다. 특정 레포의 모든 모듈/업무 기능에 대한 자동 테스트 생성은 포함하지 않는다.
- 결제·추천·검색 등 각 기능의 안정성을 주장하려면 해당 기능을 통과하는 대표 업무 경로, 초기 데이터와 필요한 외부 의존성을 준비해 별도로 검증해야 한다. 전체 API를 장애마다 전수 검사해야 한다는 의미는 아니다.
- 임의 레포가 요구하는 외부 의존성과 초기 데이터를 선언·재현하는 공통 계약을 확장한다. 결제 테스트 환경, 모델·데이터, 검색 엔진·색인 데이터는 그 활용 예시이며 특정 앱 전용 기능 개발을 뜻하지 않는다. 실제 운영 계정·운영 데이터를 자동으로 가져오지 않는다.
- 여러 업무 경로를 묶어 시나리오별로 선택하고, 보고서에 검증한 경로와 미검증 기능을 구분하는 계약은 후속 설계 사항이다. 현재 단일 probeSpec의 순차 steps를 독립 업무 경로별 평가 결과로 과장하지 않는다.
- 장애 전파/격리 검증: 한 서비스에 장애를 넣는 동안 서비스별 독립 업무 probe를 병행 관측해 의존 경로와 무관한 경로의 성공률·지연·복구를 비교한다. 현재 peer Pod 상태/자원 진단은 이 업무 기능 검증을 대체하지 않는다. `dependsOn`은 배포 순서이며 실제 의존 관계 자동 발견이나 장애 전파 검증 결과가 아니다.
- 이번 PR 본문에는 실제 실행한 업무 경로·장애 대상·부하 단계와 외부 연동 제외 범위를 함께 기재한다. 멀티레포·EC2 요청 배정·자동 기동/종료는 계속 별도 백로그로 둔다.

## 정리 제한시간

여러 JVM 앱과 실험 리소스를 삭제한 실행에서 `kubectl delete --wait`의 실패 반환과 실제 namespace 부재가 함께 관측됐다. 단순 대기 증가 후에도 반복돼 실제 부재를 직접 확인하도록 보완했다. 삭제 요청은 `--wait=false`로 보내고 namespace 조회가 실제 부재를 확인할 때까지 최대 120초 기다린다. 하위 kubectl 호출도 남은 예산과 15초 중 작은 값으로 제한한다. 빌드/테스트 기본 600초와 배포 단계 1200초는 유지한다. 삭제를 확인하지 못하면 kind 이미지를 강제로 지우지 않고 정리 실패를 기록한다. 지연 삭제 후 이미지 재회수를 맡을 별도 청소 기능은 후속 과제다.

인증 준비503의 실제 원인과 시간 제한 분리 검증은 [인증 준비 분석](authentication-preparation.md)에 기록한다.
