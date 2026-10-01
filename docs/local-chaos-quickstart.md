# Local Chaos quickstart

This guide reproduces the QuickByte_Demo proof of concept on Docker Desktop
Kubernetes. It is a controlled local environment: do not point the manifests
or experiments at a shared cluster.

## Prerequisites

- Docker Desktop with Kubernetes enabled and one Ready node
- `kubectl`, Git, Python 3.11+
- Docker Desktop resources sufficient for QuickByte, PostgreSQL, Redis, and
  Litmus (8 GB memory or more is recommended)

## 1. Install Litmus without a global Helm installation

```powershell
./scripts/bootstrap_litmus.ps1
```

The script pins Helm `3.16.2` and Litmus chart `3.30.0`, and runs Helm in a
short-lived Docker container. Litmus itself is installed as Kubernetes
resources in the local `litmus` namespace.

## 2. Build and deploy QuickByte_Demo

```powershell
git clone https://github.com/phdcoco/QuickByte_Demo.git ..\QuickByte_Demo-integration
docker build -t codereferee/quickbyte-demo:local ..\QuickByte_Demo-integration
kubectl apply -f k8s/quickbyte-demo.yaml
kubectl -n codereferee-quickbyte rollout status deployment/quickbyte-api --timeout=210s
```

The QuickByte API needs about one minute on the local cluster because Flyway,
Hibernate, PostgreSQL, and Redis initialize during startup. `startupProbe` in
the manifest intentionally allows this before liveness checks begin.

## 3. Collect real Pod Kill evidence

```powershell
python scripts/run_pod_kill_experiment.py `
  --namespace codereferee-quickbyte `
  --deployment quickbyte-api `
  --service quickbyte-api `
  --service-port 8080 `
  --label-selector app.kubernetes.io/name=quickbyte-api `
  --baseline-probes 20 `
  --recovery-timeout-seconds 180 `
  --output data/actual/quickbyte-pod-kill-001.json
```

The JSON result preserves baseline and recovery probes, replica count, probe
configuration, original/replacement Pod UIDs, Kubernetes events, application
logs, abort state, and explicit error-rate denominator.

## Cleanup

```powershell
kubectl delete namespace codereferee-quickbyte
```

To remove local Litmus resources as well, run the same Helm container command
with `helm uninstall litmus --namespace litmus`, or use the documented project
cleanup script when it is added.
