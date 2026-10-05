# kind 기반 Sandbox 런타임

CodeReferee Sandbox는 개발자의 전역 Kubernetes context를 바꾸지 않고, 요청에 사용할 cluster context를 명시적으로 선택한다.

## 준비

Docker Desktop을 실행하고 `docker`, `kubectl`을 PATH에서 사용할 수 있어야 한다. Docker Desktop 자체 Kubernetes는 켤 필요가 없다. `kind`가 없다면 고정 버전 바이너리를 프로젝트의 무시된 `.runtime/tools` 경로에 설치한다.

clone 후 전체 런타임은 한 명령으로 준비할 수 있다.

```powershell
. ./scripts/bootstrap_local.ps1
```

이 명령은 kind 설치, cluster 생성, Litmus operator 및 fault 설치를 순서대로 수행하며 이미 준비된 항목은 재사용한다.

각 단계를 따로 실행해야 할 때만 아래 명령을 사용한다.

```powershell
./scripts/install_kind.ps1
```

PowerShell에서 스크립트를 dot-source하면 kind cluster를 만들고 현재 터미널에 환경 변수를 유지한다.

```powershell
. ./scripts/bootstrap_kind.ps1
```

Git Bash에서는 cluster를 준비한 뒤 아래 변수를 직접 설정한다.

```bash
powershell -ExecutionPolicy Bypass -File scripts/bootstrap_kind.ps1
export PATH="$PWD/.runtime/tools:$PATH"
export CODEREFEREE_CLUSTER_PROVIDER=kind
export CODEREFEREE_KIND_CLUSTER_NAME=codereferee
export CODEREFEREE_KUBECTL_CONTEXT=kind-codereferee
export CODEREFEREE_KIND_COMMAND="$PWD/.runtime/tools/kind.exe"
```

기존 Docker Desktop Kubernetes 등을 계속 사용하려면 `CODEREFEREE_CLUSTER_PROVIDER=existing`으로 두고 `CODEREFEREE_KUBECTL_CONTEXT`에 사용할 context를 지정한다.

## Litmus 설치

ChaosCenter 전체 UI/서버를 설치하지 않는다. 고정된 공식 Git commit에서 Chaos operator `3.29.0`과 현재 사용하는 Pod Delete/Container Kill fault `3.30.0`만 `.runtime`에 내려받아 설치한다.

```powershell
./scripts/bootstrap_litmus.ps1 -Context kind-codereferee
```

설치 파일은 `.runtime/litmus`에만 저장되며 커밋되지 않는다. 원본 kubeconfig와 전역 current-context는 변경하지 않는다.

## Repository 배포

Repository 배포기는 다음 순서로 동작한다.

1. repository clone 및 선택적 `patchDiff` 적용
2. 사전 등록 profile 또는 `.codereferee/validation.yaml`의 `deploymentProfile` 선택
3. Docker image build
4. kind 사용 시 `kind load docker-image`로 요청 이미지를 node에 적재
5. 요청 전용 namespace에 배포하고 Chaos 실행
6. evidence 반환 후 namespace 정리

현재 `.codereferee/validation.yaml`은 임의의 배포 구성을 자동 추론하지 않고, Sandbox에 등록된 profile 이름을 선택한다. Dockerfile/Kubernetes YAML/Compose 자동 탐색은 후속 과제로 관리한다.

QuickByte 전체 경로를 로컬에서 한 번 검증하려면 다음을 실행한다. 전체 결과는 gitignore된 `.runtime`에 저장되고 터미널에는 요약만 출력된다.

```powershell
python scripts/verify_repository_pipeline.py `
  --repository-url https://github.com/phdcoco/QuickByte_Demo.git `
  --branch main `
  --request-id local-kind-001 `
  --chaos-mode litmus_pod_delete `
  --deployment-profile quickbyte-demo `
  --output .runtime/evidence/local-kind-001.json
```
