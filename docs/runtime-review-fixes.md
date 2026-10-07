# Runtime Chaos PR 리뷰 수정·검증

## 수정 사항

1. `PROBE_ERROR` 및 runtime/helper/setup 오류는 주입 이력과 무관하게 `infrastructure_error`로
   분류한다. `exitCode`와 `recovered`는 `null`이며 사용자 복구 실패로 보고하지 않는다.
2. 유효한 관측의 복구 여부는 Litmus verdict가 아니라 독립 HTTP probe의 마지막 5개 실제
   샘플로 결정한다. HTTP 미복구는 `observed / exitCode: 1 / recovered: false`로 유지한다.
3. Litmus가 반환하는 `Error`도 종료 verdict로 읽어 timeout까지 기다리지 않고 오류를 전달한다.
4. kind 이미지 로드 시도를 namespace 생성과 별도로 추적한다. 템플릿 누락·렌더링 실패뿐 아니라
   일부 노드 로드 후 실패한 경우에도 회수한다. namespace 삭제가 실패해 Pod가 남아 있으면
   사용 중일 수 있는 노드 이미지를 제거하지 않는다.

## 검증 결과 (2026-10-07)

- 수정 브랜치 회귀 테스트 38개 통과.
- 실제 Container Kill: `observed / exitCode: 0 / recovered: true`, 재시작 3→4 확인.
  이번 빠른 fixture의 장애 구간은 HTTP 샘플에 잡히지 않아 recovery_seconds는 0이었다.
  이는 무중단·정확히 0초 복구를 입증한 값이 아니다.
- 실제 EOT command probe에 존재하지 않는 명령을 지정해 `CMD_PROBE_ERROR`를 재현했다.
  Litmus는 `verdict: Error / phase: Error`를 반환했고 독립 HTTP는 정상 상태였다.
  Sandbox는 `infrastructure_error / exitCode: null / recovered: null`로 반환했다.
- 실제 오류 결과를 변경 없는 Sandbox HTTP adapter·AI parser·workflow에 재생했다.
  null exitCode가 유지됐고 Judge를 생략하며 `status: error` 결과 이벤트가 생성됐다.
  재생의 preflight·queue·artifact 저장은 테스트 대역이다. live HTTP·Redis·BE 전체 통합
  실행이라고 주장하지 않는다. LLM API는 호출하지 않았다.
- 기존 Backend `ResultQueueConsumerTest` 통과. Backend의 `error / infra_error`는 `ERROR`로
  매핑되며 AI·Backend 소스는 수정하지 않았다.
- 실제 이미지 build·kind load 후 의도적으로 manifestTemplate을 누락시켜 실패시켰다.
  namespace는 생성되지 않았고 호스트·kind 노드에서 해당 요청 이미지가 모두 제거됐다.
- 렌더링 실패, 부분 노드 로드, build 실패, namespace 삭제 실패는 회귀 테스트로 검증했다.

실측 원본과 검증 harness는 로컬 `.runtime/`에 보존하며 PR에 원본 로그·임시 프로필은 추가하지 않는다.
명령 probe는 [Litmus 공식 문서](https://litmuschaos.github.io/litmus/experiments/concepts/chaos-resources/probes/cmdProbe/)를
참고하되 설치된 3.x CRD의 duration 문자열 형식(`1s`)에 맞춰 재현했다.

## 후속 과제

- 실제 오류 이벤트를 live Redis·Backend DB·조회 API까지 통과시키는 배포 환경 합동 재검증.
- 여러 노드에서 중간 로드가 실제 실패하는 장애 재현(현재 제어 흐름 회귀 테스트로 검증).
- `STATUS_CHECKS_ERROR`를 사용자 장애로 해석할 때 필요한 주입 증거 강화.
  단순 `targeted` 이력은 성공적인 주입·복원을 입증하지 않으므로 임의로 확대 해석하지 않는다.
- 강제 종료로 finally가 실행되지 않는 경우의 TTL/GC, 노드 이미지 저장 공간 한도.
- 자동 탐색 확대·멀티서비스·멀티레포·전용 Worker·메트릭 연결은 별도 기능 작업이다.
