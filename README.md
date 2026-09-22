# codereferee-sandbox

CodeReferee의 Kubernetes 기반 실행 및 카오스 관측 컴포넌트입니다.

## 문서

- [Chaos v1 Sandbox 계약](docs/chaos-v1-contract.md)

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

Chaos v1 요청은 다음과 같습니다. 현재는 요청에 포함된 레포지토리 대신 제어된
fixture 서비스에 Pod Kill 실험을 수행합니다.

```bash
curl -X POST http://127.0.0.1:8100/repositories/validate \
  -H "Content-Type: application/json" \
  -d '{"repositoryUrl":"https://github.com/example/repository","branch":"main","requestId":"local-chaos-001"}'
```
