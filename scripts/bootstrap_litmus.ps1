$ErrorActionPreference = "Stop"

# Helm is intentionally run in a short-lived Docker container so contributors
# do not need a global Helm installation on Windows.
$helmImage = "alpine/helm:3.16.2"
$litmusChartVersion = "3.30.0"
$kubeDirectory = Join-Path $env:USERPROFILE ".kube"
$kubeConfig = Join-Path $kubeDirectory "config"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "kubectl was not found. Enable Kubernetes in Docker Desktop first."
}
if (-not (Test-Path $kubeConfig)) {
    throw "Kubernetes config was not found at $kubeConfig. Enable Docker Desktop Kubernetes first."
}

kubectl config current-context
kubectl get nodes

$mount = "$kubeDirectory`:/root/.kube:ro"
$command = "helm repo add litmuschaos https://litmuschaos.github.io/litmus-helm/ >/dev/null && helm upgrade --install litmus litmuschaos/litmus --version $litmusChartVersion --namespace litmus --create-namespace --wait --timeout 10m"
docker run --rm -v $mount --entrypoint /bin/sh $helmImage -c $command

kubectl -n litmus get pods
