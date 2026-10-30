# 레포 실행 계획과 자동 탐색

이 기능은 등록된 QuickByte 프로필 외의 실행 가능한 HTTP 서비스를 재현하기 위한 경로다.
URL만으로 모든 비밀 값·외부 서비스·MSA 연결을 추측하는 기능은 아니다.

## 선택 순서

1. 요청의 명시적 deploymentProfile (기존 계약 유지)
2. 레포의 `.codereferee/validation.yaml` (사용자 의도가 자동 추정보다 우선)
3. Compose → Dockerfile → 지원 스택의 보수적인 자동 탐색
4. 정보 부족이면 설정 필요 사유 반환

사용자가 처음부터 YAML을 반드시 작성할 필요는 없다. 자동 탐색에 실패했을 때 보완한다.
명시적 `chaosMode: fixture`만 공통 fixture를 실행한다. 모드를 생략한 일반 요청은
실제 레포의 build·배포·HTTP smoke를 진행하며 Chaos는 주입하지 않는다.
clone·patch 후 같은 clone에서 격리된 컨테이너로 install/build/test를 수행한다.
지원 스택은 Node, Python, Gradle, Maven이다. 각 단계의 exit code·시간·제한된 로그를
`sandboxReport.steps`에 남긴다. 테스트가 없으면 `test_execution: not_declared`,
Gradle/Maven XML 결과가 0개이면 `no_tests_collected`로 구분한다. 명령의 성공은
`command_passed`로 표현하며 테스트의 충분성이나 SRE 합격 판정을 대신하지 않는다.
실패하면 배포·Chaos 전에 중단한다. 테스트가 없는 앱의 HTTP/Chaos 관측은 가능하지만
테스트를 통과했다고 보고하지 않으며 최종 해석은 AI 정책 영역이다.

## YAML

기존 프로필 선택은 그대로 지원한다.

```yaml
deploymentProfile: quickbyte-demo
```

직접 선언할 때는 레포 내부 Dockerfile을 사용한다. Dockerfile이 없으면 지원되는
단일 서비스 스택은 생성 recipe를 사용할 수 있다. 재현할 포트·환경이 모호하면 보완을 요청한다.

```yaml
version: 1
service:
  dockerfile: Dockerfile
  buildContext: .
  port: 8000
  healthPath: /health
  replicas: 1
  resources:
    cpu: 1000m
    memory: 512Mi
  env:
    REDIS_HOST: redis
dependencies:
  redis:
    type: redis
    probePath: /business-endpoint
```

`command`/`args`는 문자열이 아니라 exec 배열이다. env는 재현용 값을 직접 선언한다.
개발자의 환경이나 `.env` 파일을 읽거나 `${SECRET}`를 호스트 값으로 치환하지 않는다.
PostgreSQL/MySQL password는 매 실행 새로 생성하며 다음과 같이 앱에 같은 값을 전달할 수 있다.

```yaml
version: 1
service:
  port: 8080
  dockerfile: Dockerfile
  env:
    DB_HOST: "{{dependency.db.host}}"
    DB_USER: "{{dependency.db.username}}"
    DB_PASSWORD: "{{dependency.db.password}}"
    DB_NAME: "{{dependency.db.database}}"
dependencies:
  db:
    type: postgres
```

이 값은 운영 계정이 아니라 요청 전용 재현 데이터베이스다. 외부 OAuth·결제 API
인증을 임의 값으로 대체하거나 운영 DB에 접속하지 않는다. env 값/렌더된 manifest는
실행 계획 요약에 복사하지 않는다.

## 자동 탐색의 현재 범위

- Compose: build 가능한 HTTP 서비스 하나와 단순 Redis 의존성. 여러 앱 후보이면 대상 선언 필요.
  Compose target 포트와 Dockerfile EXPOSE가 충돌하면 임의로 채택하지 않고 설정을 요청한다.
  build 대상이 하나도 없는 Compose(DB만 띄우거나 배포된 앱 이미지를 당겨 쓰는 파일)는 이 소스를 어떻게
  빌드하는지 말하지 않으므로 건너뛰고 아래 Dockerfile·스택 탐색으로 넘어간다. 이때 Compose의 의존성은
  가져오지 않는다. 앱이 DB 없이 기동하지 못하면 기동 실패로 나온다(2026-10-29 실제 레포 230개 사전 점검에서
  spring-petclinic-rest, etherpad-lite, uptime-kuma 등 14개가 이 규칙에 막혀 있었다).
- Dockerfile: 정확히 한 EXPOSE HTTP 포트. EXPOSE는 HTTP임을 보증하지 않아 실제 probe로 검증한다.
- Node: package.json의 start 또는 dev 스크립트, 기본 PORT=3000. 엔트리 파일(start 스크립트의 `node`/`nodemon` 대상,
  `main`, `server.js`·`app.js`·`index.js` 순)이 `process.env.PORT`를 읽지 않고 1024 이상 포트를 정확히 하나 박아 두었으면
  그 포트를 쓴다(node-role-based-authorization-api의 4000). 둘 이상이면 고르지 않고 3000으로 두며, 그때는 YAML 필요.
  Node 이미지는 `.nvmrc`(또는 `.node-version`)의 주 버전, 없으면 `engines.node`의 하한을 따른다. 20보다 낮추지 않고 26을 넘지 않는다.
- Python: 제한된 Flask/FastAPI 진입점. factory/복잡한 패키지는 명시적 Dockerfile 필요.
  Flask는 `--app` 대신 `FLASK_APP`으로 넘겨 레포가 고정한 Flask 1.x에서도 기동한다.
  설치할 의존성이 있으면 `build-essential pkg-config`를 먼저 깐다. 휠이 없는 패키지(multidict, PyICU)가 slim에서 설치되지
  않았다. 검사 단계는 root가 아니라 apt를 못 쓰므로 `python:3.12-bookworm`을 쓴다(Node 검사와 같은 계열).
  Poetry `package-mode = false` 프로젝트는 패키지가 아니라 `pip install .`이 실패한다. `[tool.poetry.dependencies]`를
  pip 요구사항으로 바꿔 설치한다(`^`·`~`는 Poetry 상한 규칙대로). 검사 단계는 group 의존성(pytest 등)도 깐다.
  git·path 의존성은 바꾸지 않고 설정을 요청한다.
- Spring Boot: 단순 루트 Gradle/Maven 실행 가능한 jar 생성. 멀티모듈 산출물·실행 대상은 추가 선언/확장 필요.
  루트 pom에 `<modules>`가 있고 루트 `src/main`이 없으면(spring-guides/tut-rest) 빌드 뒤 jar를 못 찾는 대신
  처음부터 설정을 요청한다. 모듈마다 앱이 있을 때 어느 것을 띄울지 고르지 않는다.
  Gradle wrapper와 pom.xml이 함께 있으면(spring-petclinic) 배포와 검사 모두 Gradle을 쓴다.
  JDK는 레포가 선언한 Java 버전(build 파일, `gradle/libs.versions.toml`, `gradle.properties`)에서 가장 높은 값을
  17·21·25 중 그 이상인 가장 낮은 버전으로 맞춘다. Gradle toolchain은 정확히 같은 JDK가 있어야 빌드된다.
- 로컬 DB 자동 재현(`scripts/local_databases.py`): 선언이 없는 레포(Dockerfile·스택 탐색 경로)의 설정이
  `localhost`/`127.0.0.1`의 MySQL·PostgreSQL·MongoDB·Redis를 가리키면, 레포에 적힌 개발용 계정·DB 이름 그대로
  임시 DB를 띄운다. 2026-10-30 실제 레포 배치에서 판정 불가 32건 중 10건이 DB가 없어서였고, 10건 모두 이 모양이었다.
  - 보는 곳: Spring `src/main/resources/application*.properties|yml`의 `spring.datasource.*`·`spring.data.mongodb.*`·
    redis 호스트, Node 설정 JSON(패키지가 `mysql`/`mysql2`/`pg`일 때)과 `mongodb://`·`redis://` 문자열, `*.ini`의
    `Type`/`User`/`Password`/`Host`/`Name` 섹션. `node_modules`·`test` 등은 건너뛴다.
  - 원격 호스트, 기본값 없는 자리표시자(`${DB_PASSWORD}`), 기본 포트가 아닌 DB는 재현하지 않는다. 값을 지어내지 않는다.
  - 배포: DB는 별도 Deployment로 두고 앱 Pod에 `local-db-forward` 사이드카(`python:3.12-alpine`)를 붙여 `localhost:<포트>`를
    DB Service로 넘긴다. DB를 사이드카로 넣으면 앱 Pod 삭제 실험이 DB까지 지워 실험의 의미가 바뀐다.
  - 검사(테스트) 단계: DB 컨테이너를 먼저 띄우고 준비될 때까지(최대 180초) 기다린 뒤, 검사 컨테이너가 그 네트워크를 함께 써서
    `localhost`로 붙는다. 끝나면 지운다. DB가 뜨지 않으면 인프라 오류(판정 불가)다.
  - MySQL은 `mysql:8.0` + `mysql_native_password`. 예제 레포가 `useSSL=false`로 붙어 MySQL 8 기본 인증이
    `Public Key Retrieval is not allowed`로 끊기고, Node `mysql` 패키지는 기본 인증을 지원하지 않는다.
  - 환경변수로만 주소를 받는 레포(msdocs-nodejs-mongodb의 `MONGODB_URI`)는 아직 재현하지 않는다.
- Kubernetes manifest 자동 변환·멀티레포 그룹·실제 온디맨드 Worker는 아직 이 단일 서비스 단계에 포함되지 않음.

Compose의 bind mount·영속 데이터·호스트 Docker socket은 재사용하지 않는다.
호스트 의존 command, privileged, env_file, secret 등은 자동 추정하지 않고 설정을 요청한다.
clone 밖의 빌드 경로와 `.git` 내부 경로를 금지한다. 제출 코드/Dockerfile 자체는 실행되므로
namespace만으로 악성 코드의 보안 격리가 완성됐다고 보지 않는다. 운영에서는 전용 Worker와 접근 제한이 필요하다.

명시적 `dependencies`는 `redis`, `postgres`, `mysql`을 지원한다. MySQL은 `mysql:8.4`의
요청 전용 빈 DB이며 앱 계정과 root 비밀번호를 각각 생성한다. 개발자의 `.env`를 읽지 않는다.
`{{dependency.db.host}}`, `port`, `username`, `database`, `password`로 앱 환경 변수에 연결한다.
빈 DB에는 기존 서비스 데이터가 없으므로 스키마 생성·seed·검증 API는 프로젝트의 재현 설정에 맞아야 한다.
여러 DB 중 장애 타깃을 임의로 선택하지 않는다.
배포는 앱 Deployment에 이어 렌더링된 모든 의존성 Deployment의 rollout도 기다린다. 앱의 healthPath는
보통 의존성을 쓰지 않아 먼저 Ready가 되므로, 기다리지 않으면 의존성 시나리오가 `No Ready Pod`로 깨진다
(2026-10-19 실측, postgres 이미지를 캐시한 뒤에도 재현). 의존성 이미지와 설정은 Sandbox가 정하므로
의존성 rollout 실패는 `failed_step: dependencies`, `infrastructure_error`로 반환한다.

Windows에서도 clone별 `core.autocrlf=false`로 원격 LF를 보존하며 전역 Git 설정은 변경하지 않는다.

## 실패와 호환성

- build/기동 실패: 관측된 사용자 실행 실패를 `sandboxReport.failed_step`과 로그로 구분.
- 설정 부족: configuration_required를 보존하고 실행 불가를 성공으로 처리하지 않음.
- Docker/클러스터 준비 오류: infrastructure_error, exitCode null.
- 정상 HTTP smoke: sandbox-result.v1, Chaos evidence를 가짜로 생성하지 않음.
- 기존 AI parser의 sandboxReport/source와 observationStatus를 사용하며 AI 정책은 수정하지 않음.

## 검증 명령 선언과 멀티모듈 산출물

자동 검증 명령이 맞지 않으면 동일 YAML의 `verification`으로 명시한다. 기존 AI 계약의
최상위 `test`(단순 명령 문자열/exec 배열) 및 Python `testDependencies` 파일 경로도 읽는다.
`verification_declared`는 배포 프로필 유무가 아니라 명시적 테스트 명령 유무다.

```yaml
version: 1
verification:
  stack: gradle
  workingDirectory: .
  build: [sh, ./gradlew, ':api:bootJar', --no-daemon]
  test: [sh, ./gradlew, ':api:test', --no-daemon]
  env:
    SPRING_PROFILES_ACTIVE: test
service:
  port: 8080
  healthPath: /health
  artifactPath: api/build/libs/application.jar
```

`artifactPath`는 실제 build가 만든 JAR 하나를 buildContext 안에서 선택한다. 임의의 JAR를
골라 실행하지 않는다. runtime 이미지만 생성하므로 같은 앱을 다시 컴파일할 필요가 없다.
이는 한 실행 서비스의 멀티모듈 산출물 선택이지 여러 MSA 서비스·멀티레포 그룹 배포가 아니다.
검증 workingDirectory·실행 명령·artifact는 해당 프로젝트에 맞게 지정해야 한다.

검증 명령은 호스트 shell에서 실행하지 않는다. 컨테이너마다 CPU 2·메모리 2GiB·PID 512 제한과
capability 제거를 적용한다. 검증 단계 전체의 기본 시간 한도는 600초다. 패키지 캐시는
요청 전용 Docker 볼륨에 두고 검증 종료 후 제거한다. timeout 시 명명된 검증 컨테이너만
정리하며 정리 실패를 숨기지 않는다. 임시 clone 삭제도 읽기 전용 Git 파일을 처리하고
`workspaceCleanup`/`source.workspace_cleanup`에 실제 결과를 기록한다.

이 제한만으로 악성 코드의 운영 보안 격리가 완성되는 것은 아니다. 전용 Worker·네트워크 제한은
별도 작업이며 아직 현재 단일 Worker API는 요청을 직렬 처리한다.

## 진행 기록

추가된 단위/계약 테스트는 실행 계획 선택, 경로 탈출, env 안전 처리, replica/resource 범위,
Compose 대상 모호성, 자동 API 분기, 일반 서비스 검증 분기 등을 확인한다.
실제 성공/실패 사례와 완료 범위는 이후 실측 기록으로 별도 추가한다.

[Node·Python/Redis·Whik 실제 API 검증 기록](repository-execution-validation.md)을 참고한다.
