# Chaos v1 Sandbox 계약

## 목적

이 문서는 `codereferee-sandbox`가 만드는 첫 번째 실제 Kubernetes 카오스 관측
결과의 형식을 정의한다. 기존 AI Core의 `SandboxResult` 응답 필드와 호환성을
유지하면서, 카오스 실험의 원시 관측 데이터를 확장 필드로 제공한다.

Sandbox는 관측된 사실만 반환한다. SLO 통과 여부와 최종 판정은 AI Core의
책임이다.

## 범위

Chaos v1은 제어된 Kubernetes fixture 서비스 하나를 검증한다.

1. 정상 상태(Baseline)를 수집한다.
2. 애플리케이션 Pod 하나를 삭제한다.
3. 대체 Pod 생성과 Ready 상태 복구를 관측한다.
4. HTTP probe 결과, Kubernetes 이벤트, 로그를 수집한다.
5. 구조화된 결과를 반환한다.

Chaos v1에는 LitmusChaos, Prometheus, 임의 레포지토리 배포, 다중 서비스
오케스트레이션, Backend Redis 이벤트를 포함하지 않는다.

### Fixture를 사용하는 이유와 한계

Chaos v1은 사용자 레포지토리를 실제로 실행하지 않는다. 대신 정상 상태와 장애
후 기대 결과를 알고 있는 fixture 서비스를 사용한다. 이를 통해 Pod 삭제, 복구,
관측 데이터 형식, AI 판단 파이프라인을 재현 가능하고 안전하게 검증한다.

임의 레포지토리 검증에는 clone, 스택 감지, 이미지 build, 환경변수·Secret 처리,
의존성 서비스 배포, 네트워크·권한 격리, 자원 정리가 추가로 필요하다. 이는
Chaos v1의 범위가 아니라 이후 Runner 확장 단계의 범위다.

따라서 Chaos v1 결과는 카오스 관측 데이터 파이프라인의 실제 검증 자료이며,
사용자 레포지토리 SRE 검증 기능이 완성됐다는 의미는 아니다.

## 요청 형식

향후 HTTP 엔드포인트는 AI Core가 이미 사용하는 필드를 받는다. `requestId`는
하위 호환성을 위해 선택값이며, 전달된 경우 응답에 그대로 포함한다.

최종 Backend-AI-Sandbox 연동에서는 Backend가 발급한 `requestId`로 한 요청의
진행 상태와 최종 결과를 연결한다. 현재 AI Core의 기존 Sandbox HTTP 호출은 이
필드를 아직 보내지 않을 수 있으므로 Chaos v1에서는 선택값으로 둔다.

```json
{
  "repositoryUrl": "https://github.com/example/repository",
  "branch": "main",
  "commitSha": "0123456789abcdef",
  "requestId": "optional-correlation-id"
}
```

Chaos v1에서는 아직 사용자가 입력한 레포지토리를 실제로 실행하지 않는다.
AI Core가 보내는 요청 형식을 나중에도 그대로 사용할 수 있도록 레포지토리 필드는
유지한다. 대신 이 저장소에 미리 준비한 테스트용 서비스(fixture)를 실행하고
Pod Kill과 복구 관측을 수행한다.

## 응답 형식

### 기존 AI Core 호환 필드

현재 AI Core HTTP adapter가 지원하는 camelCase 필드를 유지한다.

```json
{
  "exitCode": 0,
  "observationStatus": "observed",
  "stdout": "fixture recovered after pod deletion",
  "stderr": "",
  "timedOut": false,
  "durationMillis": 12000,
  "serverStarted": true,
  "serverUrl": "http://fixture-api.codereferee-sandbox.svc.cluster.local",
  "httpStatus": 200,
  "browserLoaded": false,
  "serviceCheckAttempted": true,
  "browserCheckAttempted": false,
  "pageTitle": null,
  "runCommand": ["kubectl", "delete", "pod", "fixture-api-abcde"],
  "probeTransport": "kubectl_port_forward"
}
```

`observationStatus`는 실행 결과를 신뢰할 수 있는지와 실패 책임을 구분한다.

| 값 | 의미 | `exitCode` | Backend 해석 |
| --- | --- | --- | --- |
| `observed` | 실험이 수행되어 관측 결과를 확보함 | `0` 또는 `1` | `PASSED` 또는 `FAILED` |
| `infrastructure_error` | Sandbox, Kubernetes, `kubectl`, port-forward 문제로 관측 불가 | `null` | `ERROR` |

`observed`와 `timedOut: true`는 복구 관측 시간이 초과되어 `exitCode: 1`인 검증 실패를 뜻한다. `infrastructure_error`와 `timedOut: true`는 Sandbox 실행 자체의 시간 초과를 뜻한다. HTTP 4xx는 요청 오류에만 사용하며, 구조화된 결과를 만들 수 없는 예상 밖의 API 오류만 HTTP 5xx로 반환한다.

`serviceCheckAttempted`와 `browserCheckAttempted`는 각각 HTTP 서비스 검사와 브라우저 검사를 실제로 수행했는지 나타낸다. Chaos v1은 HTTP probe만 수행하므로 전자는 `true`, 후자는 `false`다. `browserLoaded: false`는 브라우저 검사를 실패했다는 뜻이 아니라, 브라우저 검사를 실행하지 않았다는 뜻이다.

`probeTransport`은 HTTP 관측 경로를 나타낸다. Chaos v1의 HTTP 오류율과
지연시간은 외부 Ingress가 아니라 로컬 `kubectl port-forward` 기준 측정값이다.

### Chaos v1 확장 필드

현재 AI Core 응답 parser는 알 수 없는 필드를 무시하므로, 아래 확장은 하위
호환된다. 이후 AI Core는 이 필드를 SRE metrics와 evidence 모델에 보존해야 한다.

즉 Chaos v1에서 확장 필드를 반환해도 기존 AI의 `exitCode`, `stdout`,
`timedOut` 기반 판정은 깨지지 않는다. 다만 현재 AI Core가 `metrics`와
`chaos_observation`을 자동으로 Judge/Critic 판단에 사용한다는 뜻은 아니다.
AI Core는 후속 작업에서 두 필드를 모델과 evidence 변환 과정에 추가해야 한다.

```json
{
  "schemaVersion": "chaos-v1",
  "observationStatus": "observed",
  "replicas": 1,
  "requestId": "optional-correlation-id",
  "baseline": {
    "pod": {
      "name": "fixture-api-abcde",
      "ready": true,
      "restart_count": 0
    },
    "metrics": {
      "availability": 1.0,
      "p95_latency_ms": 20
    }
  },
  "metrics": {
    "availability": 0.82,
    "p95_latency_ms": 420,
    "error_rate": 0.18,
    "cpu_usage_percent": null,
    "memory_usage_mb": null,
    "restart_count": 0,
    "recovery_seconds": 12
  },
  "chaos_observation": {
    "type": "pod_kill",
    "target_kind": "Pod",
    "target_name": "fixture-api-abcde",
    "target_pod_uid": "pod-uid",
    "namespace": "codereferee-sandbox",
    "replicas": 1,
    "kill_method": "kubectl_delete_pod",
    "started_at": "2026-09-22T05:00:00Z",
    "replacement_pod_created": true,
    "replacement_pod_name": "fixture-api-fghij",
    "replacement_pod_uid": "replacement-pod-uid",
    "recovered": true
  },
  "source": {
    "real_execution_observed": true,
    "fixture": "fixture-api"
  }
}
```

`cpu_usage_percent`, `memory_usage_mb`는 Chaos v1에서 `null`일 수 있다. Prometheus
또는 Kubernetes metrics 연동 후 실제 값으로 채운다.

## 데이터 책임

| 구성 요소 | 책임 |
| --- | --- |
| Sandbox | 실행·복구·로그·관측의 원시 사실을 생성한다. |
| AI Core | SLO 정책을 적용하고 근거를 판정해 Critic/Refiner 결과를 생성한다. |
| Backend | 작업 상태와 사용자용 최종 리포트를 영구 저장한다. |

## 향후 연동 경로

```text
Backend
  → Redis input queue
  → AI Core
  → Sandbox HTTP API
  → AI Core Judge/Critic/Refiner
  → Redis output queue
  → Backend
```

Sandbox는 Backend에 직접 결과를 보내지 않는다. Sandbox는 원시 실행·복구·관측
데이터를 AI Core HTTP 응답으로 반환하고, AI Core가 이를 해석한 최종 결과를
Backend에 전달한다.
