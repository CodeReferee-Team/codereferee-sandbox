# codereferee-sandbox

CodeReferee의 Kubernetes 기반 실행 및 카오스 관측 컴포넌트입니다.

## 문서

- [Chaos v1 Sandbox 계약](docs/chaos-v1-contract.md)
- [실제 Chaos evidence 배치 수집](docs/chaos-evidence-batch.md)
- [QuickByte 로컬 Kubernetes·Litmus 재현](docs/local-chaos-quickstart.md)
- [메트릭 수집 배선](docs/metrics-collection.md)
- [Mac/Linux 로컬 Sandbox 준비](docs/posix-bootstrap.md)
- [Runtime Chaos 시나리오·검사 묶음·전체 연동](docs/runtime-chaos-scenarios.md)
- [범용 레포 실행 설정·자동 탐색](docs/repository-execution-plans.md)

## Kubernetes fixture

Chaos v1은 `fixture-api`를 제어된 테스트 대상으로 사용합니다.

```bash
kubectl apply -f k8s/fixture.yaml
kubectl -n codereferee-sandbox rollout status deployment/fixture-api
```

정상 상태의 Pod·HTTP probe·로그를 수집하려면 다음을 실행합니다.

```bash
python scripts/collect_baseline.py
```

Pod Kill과 복구 관측을 실행하려면 다음을 실행합니다.

```bash
python scripts/run_pod_kill_experiment.py --baseline-probes 5
```

## HTTP API

AI Core 연동용 HTTP API는 기본적으로 `127.0.0.1:8100`에서 실행합니다.

```bash
python -m venv .venv
source .venv/Scripts/activate
python -m pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8100
```

상태 확인은 다음 명령으로 합니다.

```bash
curl http://127.0.0.1:8100/health
```

아래 fixture 요청은 레포지토리 코드 대신 제어된 fixture 서비스에 Pod Kill을 수행합니다.
사용자 레포 clone·배포 및 검사 묶음은 [Runtime Chaos 문서](docs/runtime-chaos-scenarios.md)를 참고하세요.

```bash
curl -X POST http://127.0.0.1:8100/repositories/validate \
  -H "Content-Type: application/json" \
  -d '{"repositoryUrl":"https://github.com/example/repository","branch":"main","requestId":"local-chaos-001","chaosMode":"fixture"}'
```

`chaosMode`를 생략하면 fixture가 아니라 해당 레포를 자동 탐색해 build·배포·HTTP smoke를 수행합니다.
Chaos까지 실행하려면 `chaosMode`에 검사 묶음이나 시나리오를 지정합니다. 자동 탐색이 모호하면
설정 필요 사유를 반환하며, `.codereferee/validation.yaml`로 실행 환경을 보완할 수 있습니다.
