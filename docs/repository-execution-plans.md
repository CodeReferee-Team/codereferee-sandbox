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
일반 경로의 unit test 실행은 아직 추가 단계다. 현재 `test_execution: not_attempted`를
명시하고, 실행하지 않은 테스트를 통과한 것으로 보고하지 않는다.

## YAML

기존 프로필 선택은 그대로 지원한다.

```yaml
deploymentProfile: quickbyte-demo
```

직접 선언할 때는 레포 내부 Dockerfile을 사용한다.

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
PostgreSQL password는 매 실행 새로 생성하며 다음과 같이 앱에 같은 값을 전달할 수 있다.

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
- Dockerfile: 정확히 한 EXPOSE HTTP 포트. EXPOSE는 HTTP임을 보증하지 않아 실제 probe로 검증한다.
- Node: package.json의 start 또는 dev 스크립트, 기본 PORT=3000. 실제 앱이 이 설정을 따르지 않으면 YAML 필요.
- Python: 제한된 Flask/FastAPI 진입점. factory/복잡한 패키지는 명시적 Dockerfile 필요.
- Spring Boot: 단순 루트 Gradle/Maven 실행 가능한 jar 생성. 멀티모듈 산출물·실행 대상은 추가 선언/확장 필요.
- Kubernetes manifest 자동 변환·멀티레포 그룹·실제 온디맨드 Worker는 아직 이 단일 서비스 단계에 포함되지 않음.

Compose의 bind mount·영속 데이터·호스트 Docker socket은 재사용하지 않는다.
호스트 의존 command, privileged, env_file, secret 등은 자동 추정하지 않고 설정을 요청한다.
clone 밖의 빌드 경로와 `.git` 내부 경로를 금지한다. 제출 코드/Dockerfile 자체는 실행되므로
namespace만으로 악성 코드의 보안 격리가 완성됐다고 보지 않는다. 운영에서는 전용 Worker와 접근 제한이 필요하다.

## 실패와 호환성

- build/기동 실패: 관측된 사용자 실행 실패를 `sandboxReport.failed_step`과 로그로 구분.
- 설정 부족: configuration_required를 보존하고 실행 불가를 성공으로 처리하지 않음.
- Docker/클러스터 준비 오류: infrastructure_error, exitCode null.
- 정상 HTTP smoke: sandbox-result.v1, Chaos evidence를 가짜로 생성하지 않음.
- 기존 AI parser의 sandboxReport/source와 observationStatus를 사용하며 AI 정책은 수정하지 않음.

## 진행 기록

추가된 단위/계약 테스트는 실행 계획 선택, 경로 탈출, env 안전 처리, replica/resource 범위,
Compose 대상 모호성, 자동 API 분기, 일반 서비스 검증 분기 등을 확인한다.
실제 성공/실패 사례와 완료 범위는 이후 실측 기록으로 별도 추가한다.
