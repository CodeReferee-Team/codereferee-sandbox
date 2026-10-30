# codereferee-sandbox

CodeReferee에서 레포를 실제로 실행하고, 부하와 장애를 주입해 관측하는 컴포넌트입니다. AI Core가 HTTP로 요청하면 레포를 clone해서 빌드·테스트하고, kind 클러스터에 배포한 뒤 시나리오를 돌리고 관측값을 돌려줍니다. 판정은 하지 않습니다. 판정은 AI Core의 규칙이 맡습니다.

## 요청 한 건의 흐름

```
디스크 여유 확인 → clone(+ patchDiff 적용) → 실행 계획 탐색 → 빌드·테스트 검사
→ 이미지 빌드·kind load → 배포(+ 로컬 DB 재현) → 시나리오 순서대로 실행 → 정리
```

- **실행 계획 탐색**: `.codereferee/validation.yaml`이 있으면 그 설정을 따르고, 없으면 Compose, Dockerfile, 스택(Node·Python·Maven·Gradle)에서 자동으로 찾습니다. 판단이 모호하면 추측하지 않고 "설정 필요" 사유를 돌려줍니다. 자세한 내용은 [실행 설정·자동 탐색](docs/repository-execution-plans.md)에 있습니다.
- **로컬 DB 재현**: 레포 설정이 `localhost`의 MySQL·PostgreSQL·MongoDB·Redis를 가리키면, 그 레포의 개발용 계정 그대로 임시 DB를 띄워 붙입니다.
- **헬스 판정**: 레포가 헬스 경로를 정하지 않았으면 `GET /`를 봅니다. 이때 401·404 같은 4xx도 "응답함"으로 셉니다. 5xx와 무응답만 실패입니다.
- **정리**: 요청마다 만든 namespace, 이미지, 검사 볼륨을 지웁니다.

## 시나리오와 검사 묶음

| `chaosMode` | 내용 |
| --- | --- |
| `suite_quick` | Container Kill |
| `suite_standard` | 동시 사용자 부하 → Container Kill → CPU 부하 → 네트워크 지연 |
| `suite_deep` | 동시 사용자 부하와 장애 8종 |
| `load_concurrent_users` | 동시 사용자를 10·50·100·200명으로 늘리며 오류 없이 버틴 최대 인원, 응답 시간 증가, 넘쳤을 때의 실패 방식을 잽니다 |
| 그 밖 | `litmus_pod_delete`, `litmus_pod_cpu_hog`, `litmus_pod_network_latency`, `litmus_pod_network_loss`, `litmus_pod_memory_hog`, `litmus_pod_memory_oom`, `deployment_scale_down`, `rollout_restart`, `service_selector_blackhole`, `dependency_database_outage`, `dependency_redis_outage` |

`chaosMode`를 생략하면 배포와 HTTP smoke까지만 합니다. 요청에 `targetUsers`(예상 동시 사용자 수, 1~500)를 넣으면 부하 시나리오가 그 인원을 단계에 꼭 넣어 잽니다. 시나리오별 관측 방식은 [Runtime Chaos 시나리오](docs/runtime-chaos-scenarios.md)에 있습니다.

## 실행

Mac/Linux 기준입니다. Windows는 `scripts/*.ps1`을 씁니다. 자세한 전제는 [Mac/Linux 로컬 Sandbox 준비](docs/posix-bootstrap.md)에 있습니다.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export CODEREFEREE_PYTHON="$PWD/.venv/bin/python"

# source로 실행해야 같은 셸의 uvicorn이 kind 설정을 이어받습니다.
source scripts/bootstrap_local.sh codereferee
"$CODEREFEREE_PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8100
```

```bash
curl http://127.0.0.1:8100/health

curl -X POST http://127.0.0.1:8100/repositories/validate \
  -H "Content-Type: application/json" \
  -d '{"repositoryUrl":"https://github.com/render-examples/express-hello-world","branch":"main",
       "requestId":"local-001","chaosMode":"suite_standard","targetUsers":100}'
```

`chaosMode: "fixture"`를 주면 레포 대신 제어된 `fixture-api`(`k8s/fixture.yaml`)에 Pod Kill을 수행합니다.

### 운영 서버

샌드박스 EC2에서는 systemd 서비스 `codereferee-sandbox`로 돕니다. `dev` 브랜치를 반영하는 방법은 다음과 같습니다.

```bash
cd ~/codereferee-sandbox && git pull origin dev && sudo systemctl restart codereferee-sandbox
```

빌드 캐시와 베이스 이미지는 요청이 끝나도 남습니다. 배포 전에 Docker 데이터 경로의 여유가 `CODEREFEREE_MIN_FREE_DISK_GB`(기본 5) 아래면, 빌드 캐시를 비운 뒤 다시 잽니다. 그래도 모자라면 `sandbox_disk_low` 인프라 오류로 멈춥니다. 수동으로 정리할 때는 아래를 실행합니다.

```bash
sudo docker builder prune -af
sudo docker image prune -af
sudo docker exec codereferee-control-plane crictl rmi --prune
```

## 문서

- [Chaos v1 Sandbox 계약](docs/chaos-v1-contract.md)
- [범용 레포 실행 설정·자동 탐색](docs/repository-execution-plans.md)
- [validation.yaml 작성법](docs/validation-yaml.md)
- [Runtime Chaos 시나리오·검사 묶음·전체 연동](docs/runtime-chaos-scenarios.md)
- [Litmus 전체·단일 Pod 장애 범위](docs/single-pod-chaos-scope.md)
- [앱 OOM 실험·실행 제한 계약](docs/target-app-oom.md)
- [배포 Pod 진단](docs/deployment-pod-diagnostics.md)
- [메트릭 수집 배선](docs/metrics-collection.md)
- [요청별 메트릭 파이프라인·실측](docs/metrics-request-lifecycle.md)
- [Mac/Linux 로컬 Sandbox 준비](docs/posix-bootstrap.md)
- [QuickByte 로컬 Kubernetes·Litmus 재현](docs/local-chaos-quickstart.md)
- [실제 Chaos evidence 배치 수집](docs/chaos-evidence-batch.md)
