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
