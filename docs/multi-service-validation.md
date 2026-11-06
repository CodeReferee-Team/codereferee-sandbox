# 단일 레포 멀티서비스 실행

하나의 레포에 업무 앱이 여러 개 있을 때 `.codereferee/validation.yaml`의 `version: 2`로 선언한다.
지원 범위는 업무 앱 2~5개와 재현용 Redis·PostgreSQL·MySQL이다. 모든 자원은 요청 namespace 안에 생성한다.
기존 단일 `service`의 version 1과 사전 등록 profile 경로도 유지한다.

```yaml
version: 2
entryService: gateway
targetService: orders
probePath: /api/orders
services:
  gateway:
    buildContext: services/gateway
    dockerfile: services/gateway/Dockerfile
    port: 8080
    healthPath: /health
    dependsOn: [orders]
    env:
      ORDERS_URL: '{{service.orders.url}}'
  orders:
    buildContext: services/orders
    dockerfile: services/orders/Dockerfile
    port: 8081
    healthPath: /health
    env:
      DB_HOST: '{{dependency.orders-db.host}}'
      DB_PORT: '{{dependency.orders-db.port}}'
      DB_USER: '{{dependency.orders-db.username}}'
      DB_PASSWORD: '{{dependency.orders-db.password}}'
      DB_NAME: '{{dependency.orders-db.database}}'
dependencies:
  orders-db:
    type: postgres
```

각 앱은 예시에 적힌 환경변수와 포트에 맞게 동작해야 한다. `/api/orders`는 예시이며 실제로 orders와 DB를
사용하는 gateway 경로로 바꾼다. `/health`는 각 앱의 기동 확인 경로다. 일반적인 health 응답만으로 업무 기능
정상이나 DB 접근 성공을 보장하지 않는다.

## 설정 규칙

- `entryService`: 사용자의 요청이 들어가는 앱. 반드시 명시한다.
- `targetService`: 장애 대상 앱. 생략하면 entryService다. 서비스 라우팅 장애는 이 앱의 Service selector를 바꾼다.
- `probePath`: entryService에서 관측할 경로. 생략하면 그 앱의 healthPath다.
- `dependsOn`: 먼저 배포·준비를 마쳐야 하는 다른 업무 앱 이름. 순환 참조는 거부한다. DB는 앱들보다 먼저 준비한다.
- `dockerfile`은 레포 루트 기준 경로, `buildContext`는 Docker 빌드에 전달하는 레포 내부 디렉터리다.
- 각 앱에 `replicas`, `resources`, `command`, `args`, `env`도 기존 단일 서비스와 같은 형식으로 지정할 수 있다.
- Dockerfile을 생략하면 해당 buildContext에서 지원하는 스택 레시피를 찾는다. 판단할 수 없으면 설정을 요구한다.
- 검사 기본 위치는 buildContext다. 공유 루트를 빌드하는 멀티모듈이라면 `verificationDirectory`로 검사할 모듈을
  레포 루트 기준으로 지정할 수 있다. 해당 디렉터리에 기존 verification 설정이 있으면 그 설정을 사용한다.
- 루트에 공통 `verification`을 선언하면 서비스마다 반복하지 않고 레포 전체 검증을 한 번 수행한다.
  검사 DB 및 인증이 필요한 업무 경로의 `probeSpec` 설정은 [추가 설정 안내](authenticated-multi-service.md)를 참고한다.
- `{{service.NAME.host}}`, `.port`, `.url`로 내부 Service 주소를 참조한다. 생성되는 이름은 `cr-svc-NAME`이다.
- `{{dependency.NAME.host}}`, `.port`, `.username`, `.database`, `.password`로 재현용 DB 값을 참조한다.
  같은 DB를 사용하는 앱들은 동일한 재현용 자격증명을 받는다. 운영 Secret이나 호스트 환경은 읽지 않는다.
- 여러 서비스가 있는 Compose를 자동 변환하거나 멀티레포를 합치는 기능은 이 버전에 포함하지 않는다.

## 실행과 결과

1. 요청별 clone에 patchDiff를 적용한다.
2. 공통 verification이 있으면 루트에서 한 번, 없으면 각 서비스 검사 위치에서 설치·빌드·테스트를 실행한다. 테스트 미선언은 실행한 것으로 표시하지 않는다.
3. 서비스마다 이미지를 빌드하고 kind에 적재한다.
4. DB를 준비하고 dependsOn 순서로 앱을 배포한다.
5. targetService에 장애를 넣고 entryService의 probePath로 실제 HTTP 요청을 보낸다.
6. 결과를 확보하고 namespace·모든 요청 이미지·workspace를 정리한다.

`deployment.services`는 서비스별 workload 정보를, `deployment.images`는 요청 이미지들을 담는다.
`sandboxReport.services`는 서비스별 검사 결과이며 `service_timings`에는 검증·빌드·이미지 적재·롤아웃의
실제 소요 시간이 서비스 이름과 함께 기록된다. 이 값은 완료된 실행 기록이며 사전 ETA는 아니다.
공통 verification을 사용하면 `verification_scope: repository`, `verification_service_group`와 루트 검사 결과를 반환한다.
이때 테스트 수는 실제 루트 실행 횟수 기준이며 서비스 수만큼 합산하지 않는다.
`verification_service_group`은 이 공통 검사를 사용하는 앱 목록이며 서비스별 테스트 커버리지를 자동 추론한 결과가 아니다. 실제 검사 범위는 기록된 명령과 테스트 결과로 확인한다.

각 서비스 target의 `resourceConfiguration`은 요청한 CPU·메모리 한도(`requestedLimits`)와 그 출처(`sources`)를
각각 담는다. `repository_configuration`은 레포 설정에 명시한 값, `sandbox_default`는 Sandbox 기본값,
`unknown`은 출처를 확인할 수 없는 경우다. 이는 요청한 설정의 출처이며 Admission 등의 변경까지 확인한 실제 사용량은 아니다.
실제 Pod limits·사용량과 비교해서 해석해야 하며, 출처 필드만으로 코드 결함이나 인프라 오류로 판정을 바꾸지 않는다.
서비스별 자원 출처는 `source.workload_resources`에도 전달한다. 출처가 없는 사전 배포 대상은 unknown으로 남긴다.

롤아웃에 실패하면 `sandboxReport.failed_service`와 `pod_diagnostics.service`에 해당 앱 이름을 남긴다.
정리 전에 해당 앱의 Pod 상태·로그·이벤트를 수집하고 기존 Secret 마스킹을 적용한다.

실험 중 복구 실패가 관측되면 `source.service_diagnostics`에 다른 업무 앱의 구조화된 Pod 상태도 남긴다.
예를 들어 API에 장애를 넣었더라도 gateway가 OOM으로 재시작한 것인지 구분할 근거가 된다.
이 경로에서는 렌더링 당시 Secret 값 목록을 가지고 있지 않으므로 로그·자유 텍스트를 보내지 않고 상태·종료 이유·종료 코드만 수집한다.

자원 관측을 켠 경우 기존 CPU·메모리 시계열은 장애 대상 앱 기준을 유지한다. 추가 서비스별 시계열과 Pod UID는
`source.metrics_observation.services`에 담는다. 데이터가 없으면 unavailable로 남기며 값을 만들지 않는다.
DB·여러 서비스의 사용량을 합쳐 장애 대상 앱 하나의 자원 SLO처럼 취급하지 않는다.

## 시간 제한과 현재 경계

기존 API의 전체 요청 기한과 단계 제한을 따른다. 여러 앱의 검사·빌드는 현재 순차 실행하므로 단일 앱보다 오래 걸릴 수 있다.
예상 시간만으로 실행을 사전 거절하는 기능은 없다. 전체 시간 처리는 [실행 시간·타임아웃 검토](execution-time-policy.md)를 참고한다.

namespace는 EC2 수준의 격리가 아니다. 이 기능은 같은 클러스터에 여러 앱을 배포하는 기능이며, 여러 검증 요청의 동시
배정이나 EC2 자동 생성·종료는 별도 작업이다.

실측 범위와 후속 재검증 정책 논의는 [검증 기록](multi-service-validation-results.md)에 구분해 적었다.
서비스별 진단·자원 메타데이터 전달이 AI의 version 2 자동 수정 정책까지 구현했다는 뜻은 아니다.
