# 메트릭 수집 배선

카오스 구간의 지표를 **1초 해상도로** 남기기 위한 Sandbox 쪽 배선이다. 판정에 쓸 PromQL과 임계값은 이 문서의 범위가 아니다.

## Prometheus를 Sandbox에 두지 않는 이유

세 제약이 동시에 걸린다.

1. 스파이크를 보려면 초 단위 해상도가 필요하다.
2. Sandbox는 카오스로 강제 종료될 수 있다. 저장소가 안에 있으면 보려던 구간이 같이 사라진다.
3. Sandbox 인스턴스는 요청마다 켜고 끈다. 비용 때문이다.

그래서 Sandbox 안에는 **수집만** 두고, 저장과 질의는 밖의 장수 Prometheus가 맡는다.

- Sandbox: cAdvisor + Node Exporter를 **Prometheus Agent**가 1초 간격으로 긁어 즉시 `remote_write`로 내보낸다. Agent 모드에는 TSDB도 질의 API도 없다.
- 밖: 수신 Prometheus가 저장하고 질의한다.

검토했으나 쓰지 않은 안은 다음과 같다.

- **federation(`/federate`)**: 당겨가는 간격의 값만 가져오므로 1초로 수집해도 결과가 다운샘플링된다. 2번 제약은 풀리지만 1번을 깬다.
- **밖에서 Sandbox를 직접 scrape**: Sandbox가 요청마다 생기고 사라져서 서비스 디스커버리 갱신 지연(예: `ec2_sd_config` 60초) 동안의 baseline을 놓친다. 교차 클러스터 인증도 따로 필요하다.

`remote_write`는 Pushgateway와 다르다. 수집은 여전히 pull이고, push는 장기 저장소로 넘기는 운반 구간에만 쓴다.

## 설치

```bash
export CODEREFEREE_KUBECTL_CONTEXT=kind-codereferee
python3 scripts/install_observability.py \
  --request-id <요청 id> \
  --remote-write-url http://<수신 호스트>:9090/api/v1/write
```

제거는 `--uninstall`이다. 매니페스트는 `k8s/observability.yaml` 하나이고, `${REMOTE_WRITE_URL}` / `${REQUEST_ID}` / `${CLUSTER_NAME}`를 설치 시점에 치환한다. `k8s/quickbyte-demo.yaml`과 같은 방식이다.

### 수신 측 요구사항

수신 Prometheus는 **`--web.enable-remote-write-receiver`** 로 떠 있어야 한다. 이 플래그가 없으면 `/api/v1/write`가 404를 돌려주고, Agent는 실패가 아니라 재시도로 처리해서 조용히 큐만 쌓인다. `prometheus_remote_storage_samples_failed_total`은 0인데 `prometheus_remote_storage_enqueue_retries_total`만 올라가면 이 상황이다.

로컬 확인용으로는 다음으로 충분하다.

```bash
docker run -d --name cr-recv -p 9090:9090 prom/prometheus:v2.54.1 \
  --config.file=/etc/prometheus/prometheus.yml \
  --web.enable-remote-write-receiver
```

## 요청 구분

모든 샘플에 `codereferee_request_id`와 `codereferee_cluster`가 external label로 붙는다. 이게 없으면 수신 측에서 여러 검증 실행의 샘플이 한 시계열에 섞여 구분되지 않는다.

## 1초 해상도를 지키는 설정

초 단위 해상도는 `scrape_interval: 1s`만으로는 나오지 않는다. 실측으로 확인한 두 지점이 더 있다.

**cAdvisor의 동적 housekeeping.** `--allow_dynamic_housekeeping`은 기본값이 `true`이고, 한가한 컨테이너의 수집 주기를 `--max_housekeeping_interval`(기본 1분)까지 늘린다. 카오스 직전까지 조용하던 컨테이너가 바로 스파이크를 내므로, 늘어난 주기에 걸리면 그 스파이크를 통째로 놓친다. `false`로 고정한다. 같은 이유로 `--global_housekeeping_interval`도 1초로 둔다. 기본값 1분이면 pod-delete 이후 새로 뜬 컨테이너를 1분간 발견하지 못하는데, 그 복구 구간이 바로 봐야 하는 구간이다.

**cAdvisor가 직접 박는 타임스탬프.** cAdvisor는 컨테이너별 housekeeping 고루틴에서 측정한 시각을 샘플에 넣어 보낸다. 그 시각은 scrape 간에 단조증가하지 않아서, 그대로 믿으면 Prometheus가 상당수를 out-of-order로 거부한다. 실측으로 45초 구간에 24개만 남고 평균 간격이 1.56초였다. scrape 자체는 `up` 시계열 기준 정확히 1.000초 간격이므로, `honor_timestamps: false`로 수집 시각을 쓰면 정확한 1초 격자가 된다.

두 설정은 짝으로만 성립한다. `honor_timestamps: false`만 켜고 housekeeping이 늘어나면, 같은 값이 매 scrape 반복 기록되어 스파이크가 조용히 평탄해진다.

**scrape 예산.** `scrape_interval`이 1초이므로 `scrape_timeout`도 1초를 넘길 수 없고, 한 번이라도 넘기면 그 1초가 비어버린다. 단일 노드 kind에서 실측한 `scrape_duration_seconds`는 0.27초로 예산의 1/4이다. 대상 저장소의 컨테이너가 늘면 이 값도 커지므로, 해상도를 포기하지 않으려면 keep 목록이 아니라 cAdvisor의 `--disable_metrics`로 **노출 자체를** 줄여야 한다. `metric_relabel_configs`는 scrape 이후에 걸리므로 scrape 비용을 줄여주지 않는다.

## 전송량

보낼 지표는 `metric_relabel_configs`의 keep 목록으로 좁힌다. cAdvisor는 기본적으로 컨테이너의 모든 label을 시계열 label로 복사하므로(이미지 maintainer까지 따라붙는다) `--store_container_labels=false`로 끄고 식별에 필요한 셋만 남긴다. cgroup 경로(`id`)와 컨테이너 해시(`name`), `image`도 한 샘플당 수백 바이트인데 식별에 보탬이 없어 버린다.

단일 노드 kind에서 실측한 값이다.

| 항목 | 값 |
| --- | --- |
| cAdvisor가 노출하는 `container_*` 시계열 | 4,205 |
| keep 목록 통과 후 활성 시계열 | 228 |
| 전송 속도 | 209 samples/s, 5.2 KiB/s (압축 후) |
| 10분 카오스 1회당 | 약 3 MiB |

## 권한

에이전트는 `codereferee-observability` 네임스페이스의 `pods` 조회 권한만 가진다(Role, ClusterRole 아님). 두 `kubernetes_sd_configs` 모두 이 네임스페이스로 한정되어 있고 kubelet을 직접 긁지 않으므로 그 이상이 필요 없다. cAdvisor와 Node Exporter는 API 서버와 통신하지 않아 `automountServiceAccountToken: false`로 토큰을 받지 않는다. 샌드박스는 제출된 코드를 실행하는 곳이라 쓰지 않는 권한을 남겨두지 않는다.

## 수집하는 지표

| 지표 | 출처 |
| --- | --- |
| `container_cpu_usage_seconds_total` | cAdvisor |
| `container_memory_working_set_bytes`, `container_memory_usage_bytes` | cAdvisor |
| `container_network_{receive,transmit}_bytes_total` | cAdvisor |
| `container_fs_{reads,writes}_bytes_total` | cAdvisor |
| `node_cpu_seconds_total`, `node_load1` | Node Exporter |
| `node_memory_{MemAvailable,MemTotal}_bytes` | Node Exporter |
| `node_disk_{read,written}_bytes_total` | Node Exporter |
| `node_network_{receive,transmit}_bytes_total` | Node Exporter |

컨테이너 지표에는 `namespace` / `pod` / `container` / `node` label이 붙는다. 독립 cAdvisor는 kubelet의 `/metrics/cadvisor`와 달리 이 label을 만들어 주지 않고 `container_label_io_kubernetes_*`로 내보내므로, Agent 쪽에서 표준 이름으로 환원한다.

무엇을 판정 기준으로 삼을지는 Litmus 시나리오가 확정된 뒤에 정한다. 이 배선은 그 결정과 무관하게 먼저 성립한다.

## 점검

```bash
kubectl -n codereferee-observability get pods
kubectl -n codereferee-observability port-forward deploy/codereferee-agent 19090:9090
curl -s localhost:19090/api/v1/targets | jq '.data.activeTargets[] | {job: .labels.job, health}'
```

Agent 자신의 지표에서 볼 것은 다음이다.

| 지표 | 정상값 | 올라갈 때의 뜻 |
| --- | --- | --- |
| `prometheus_target_scrapes_sample_out_of_order_total` | 0 | `honor_timestamps`가 꺼져 있지 않다 |
| `prometheus_remote_storage_samples_failed_total` | 0 | 수신 측이 샘플을 거부한다 |
| `prometheus_remote_storage_enqueue_retries_total` | 증가하지 않음 | 수신 측에 닿지 못한다(대개 receiver 플래그 누락) |
| `prometheus_remote_storage_shards` | 1 | 전송이 수집을 못 따라간다 |
