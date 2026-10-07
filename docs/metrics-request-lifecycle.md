# 요청별 메트릭 파이프라인

## 범위와 역할

Sandbox가 앱 배포 후 관측 스택을 준비하고 baseline/Chaos 중 샘플을 외부 receiver로 전송한다.
결과에 수신 상태와 Pod 식별 정보를 남기고, 관측 스택을 제거한 뒤 앱 namespace·요청 이미지를 정리한다.
메트릭 수집·수신 확인은 SLO 합격 판정이 아니다. AI Judge·PromQL 정책 및 BE 운영 대시보드는 변경하지 않는다.

메트릭 기능은 `CODEREFEREE_METRICS_REMOTE_WRITE_URL`과 `CODEREFEREE_METRICS_QUERY_URL`로 활성화한다.
두 값은 플랫폼 운영 설정이며 사용자 요청·레포 파일에서 읽지 않는다. 미설정이면 `disabled`로 기존 실행을 유지한다.
하나만 설정하거나 수집이 실패하면 `source.metrics_observation.status = error`로 남긴다.
선택 관측의 실패만으로 검증 대상의 exitCode/observationStatus를 바꾸지는 않는다. 누락된 지표를 0으로 채우지 않는다.

## 로컬 준비

Docker와 기존 `kind-codereferee`를 준비한 뒤:

```powershell
python scripts/bootstrap_metrics_receiver.py
# 출력된 두 환경변수를 API를 실행할 터미널에 설정한다.
$env:CODEREFEREE_METRICS_REMOTE_WRITE_URL='http://codereferee-metrics-receiver:9090/api/v1/write'
$env:CODEREFEREE_METRICS_QUERY_URL='http://127.0.0.1:19091'
$env:CODEREFEREE_CLUSTER_PROVIDER='kind'
$env:CODEREFEREE_KUBECTL_CONTEXT='kind-codereferee'
$env:CODEREFEREE_KIND_COMMAND=(Resolve-Path '.runtime/tools/kind.exe').Path
python -m uvicorn app.main:app --host 127.0.0.1 --port 8102
```

Mac/Linux에서는 `python3 scripts/bootstrap_metrics_receiver.py --shell bash`로 출력된 export를 사용한다.
receiver 준비 스크립트의 실제 검증은 Windows이다. Mac/Linux 실측을 대신하지 않는다.
receiver는 localhost 19091에만 공개하고 kind Docker 네트워크에 연결한다. 기존 BE Prometheus 9090과 별개다.
`--web.enable-remote-write-receiver`, 24시간 보관 및 영속 Docker volume을 사용한다.
호환되지 않는 동일 이름 컨테이너를 삭제·재생성하지 않으며, 요청 정리는 receiver나 저장 volume을 지우지 않는다.
새 receiver 생성에는 Docker Compose v2가 필요하다. 기존 receiver 재사용·Ready 경로는 실측했다.

## 수명주기

1. patched clone에서 build/test 후 요청 앱을 배포한다.
2. 요청 ID와 대상 namespace의 해시로 `codereferee-metrics-<hash>`를 생성한다.
3. cAdvisor·Node Exporter·Agent rollout 및 설정 해시 Ready를 확인한다.
4. receiver에 해당 앱 CPU·메모리의 새 원본 샘플이 도착할 때까지 최대 45초 준비한다.
5. baseline/Chaos를 수행하면서 2초 간격으로 앱 Pod name/UID/container/resource/restart metadata를 기록한다.
6. 종료 후 최대 20초 동안 CPU·메모리의 최근 원본 샘플 도착을 확인하고 관측 구간을 조회한다.
7. Agent·exporter namespace를 삭제한다. 그 뒤 요청 앱 namespace와 정확한 요청 이미지 태그를 정리한다.

PriorityClass는 공유 플랫폼 준비 자원으로 유지한다. request namespace의 Role/RoleBinding은 함께 삭제한다.
강제 프로세스 종료로 finally가 실행되지 않는 경우의 TTL/GC는 후속이다. receiver에 전달된 데이터는 남지만
Agent RAM WAL의 미전송 데이터까지 보존하거나 모두 drain했다고 주장하지 않는다.

현재 Sandbox API는 동시 한 요청만 처리하며 나머지는 409다. 요청별 Agent 분리는 설정 덮어쓰기를 피하기 위한
배선이지 API 병렬 처리·전용 worker·물리 노드 격리 구현 완료가 아니다.

## 메트릭·식별 계약

- `codereferee_request_id`, `codereferee_cluster` external label 유지: AI PR #99의 요청 ID 조회와 호환.
- 앱 시계열: `namespace`, `pod`, `container`, `node` label. 대상 Deployment의 Pod family만 전송한다.
  observer·Litmus helper·DB/Redis dependency는 앱 자원 사용량 합산에 포함하지 않는다.
- `container_cpu_usage_seconds_total`: 누적 CPU 초. `rate(...[10s])`는 코어 사용량이다.
- `container_memory_working_set_bytes`: working set bytes. limit 및 restartCount는 Kubernetes metadata snapshots에 기록한다.
- `pod_bindings`: name/UID/container, first/last observed UTC 시각, created/deletion 시각, node, resource requests/limits,
  최초/최종 restartCount, 최종 container ID. 2초 표본의 first/last observed는 정확한 생성·사망 시각이 아니다.
- Node Exporter는 공유 노드 진단값이며 사용자 앱 사용량이 아니다. 이를 사용자 앱 SLO로 자동 해석하지 않는다.

`source.metrics_observation`에는 아래를 반환한다:

| 항목 | 의미 |
| --- | --- |
| `status` | disabled / received / partial / error |
| `request_id`, `cluster`, `namespace`, `workload` | 조회 범위 |
| `started_at`, `ended_at` | UNIX seconds 기준 메트릭 관측 구간 |
| `pod_bindings` | Pod 교체 및 컨테이너/limit/restart metadata |
| `last_received_sample`, `receipt_confirmed` | CPU·메모리에 종료 시각 근처 원본 샘플이 있었는지 |
| `cpu_cores`, `memory_working_set_bytes`, `series` | 실제 receiver 조회값 및 label/평가점 수 |
| `range_step_seconds`, `range_semantics` | 1초 PromQL 평가 격자이며 1초마다 새 원본이 있었다는 보장은 아님 |
| `full_wal_drain_proven` | false: 모든 샘플 drain을 증명하지 않음 |
| `cleanup`, `errors` | 관측 namespace 제거 및 수집/조회 오류 |

`timestamp(metric)`의 값으로 원본 샘플 시각을 확인한다. query_range 응답의 평가 시각만으로 수신을 판정하면
lookback의 오래된 값을 새 샘플로 오해할 수 있다. `receipt_confirmed`도 전체 Pod·전체 구간의 무누락 보장은 아니다.
Pod 교체나 일시적인 scrape gap 때문에 종료 한 점의 instant query가 비어도 과거 구간 데이터는 남을 수 있다.
정리 후 보존 확인은 기록된 구간의 `count_over_time`으로 실제 샘플 존재를 확인한다.

설정과 조회 API는 [Prometheus 설정](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)과
[HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)를 따른다.

## AI 팀과 남은 조율

실연동은 AI main `a040a69`에서 실행했다. #99는 아직 별도 PR이며 이번 작업에서 AI 코드를 수정하지 않았다.
현재 main에서도 source 보고서가 AI parser→Redis→Backend DB/조회 API까지 보존된다.
CPU/메모리의 최종 SRE 필드를 채우고 Judge에 적용하는 것은 #99의 설정/머지 후 별도 검증이다.

#99의 실제 PromQL을 과거 실행에 적용해 라벨 호환성을 확인했다. 진단용 HTTP wrapper가 평가 시각을 실제 종료
시각으로 고정했으며, 이것을 수정 없는 #99 workflow E2E라고 주장하지 않는다.

AI 팀 확인 필요:

- 현재 시각 대신 실제 종료 시각 `time=` 및 관측 구간을 사용해 teardown/queue 지연만큼 조회가 이동하지 않도록 한다.
- CPU `cores × 100`은 1코어 대비 값이며 limit 대비 사용률이 아니다. 여러 코어면 100을 넘을 수 있다.
- Pod 교체·여러 replicas·여러 컨테이너의 합산 단위와 limit 분모를 정한다.
- 같은 job의 patch 재검증은 request ID를 재사용한다. 구간/cluster/namespace/현재 Pod bindings로 범위를 좁혀야 한다.
- 누락·partial·수집 오류를 지표 검증 완료로 표시하지 않는다. 정책과 사용자 경고 처리는 AI/FE 팀 영역이다.

## 검증 실행

실제 Frontend 프록시·Backend·Redis·AI Worker·Sandbox·receiver를 실행한 상태에서:

```bash
python scripts/verify_metrics_pipeline.py --submission-url http://127.0.0.1:5174 \
  --require-pod-replacement --output .runtime/evidence/metrics-result.json
```

AI Worker는 별도로 실행한다. 실제 DB 저장과 receiver의 정리 후 구간 샘플까지 확인한다.
스크립트는 판정을 만들거나 실패를 Pass로 바꾸지 않는다. 실측/제한은 `docs/metrics-pipeline-validation.md` 참조.
