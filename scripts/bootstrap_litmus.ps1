param(
    [string]$Context = $env:CODEREFEREE_KUBECTL_CONTEXT
)

$ErrorActionPreference = "Stop"

# Pin immutable upstream commits instead of relying on a moving chart or tag.
$operatorVersion = "3.29.0"
$operatorCommit = "97cfc6f1ee73af5f8e6b7f8c01e97b116cccfc0c"
$faultVersion = "3.30.0"
$faultCommit = "3d485c7854aeabcf136118ff54b78434a1a14099"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "kubectl was not found in PATH."
}
if (-not $Context) {
    $Context = (kubectl config current-context).Trim()
}
if ($Context -notmatch '^[A-Za-z0-9._-]+$') {
    throw "Invalid Kubernetes context: $Context"
}

kubectl --context $Context get nodes | Out-Host

$root = Split-Path $PSScriptRoot -Parent
$manifestDirectory = Join-Path $root ".runtime/litmus"
$operatorManifest = Join-Path $manifestDirectory "litmus-operator-v$operatorVersion.yaml"
$podDeleteManifest = Join-Path $manifestDirectory "pod-delete-v$faultVersion.yaml"
$containerKillManifest = Join-Path $manifestDirectory "container-kill-v$faultVersion.yaml"
$runnerRbacManifest = Join-Path $root "k8s/litmus-runner-rbac.yaml"
New-Item -ItemType Directory -Path $manifestDirectory -Force | Out-Null

$downloads = @(
    @(
        "https://raw.githubusercontent.com/litmuschaos/litmus/$operatorCommit/mkdocs/docs/litmus-operator-v$operatorVersion.yaml",
        $operatorManifest
    ),
    @(
        "https://raw.githubusercontent.com/litmuschaos/chaos-charts/$faultCommit/faults/kubernetes/pod-delete/fault.yaml",
        $podDeleteManifest
    ),
    @(
        "https://raw.githubusercontent.com/litmuschaos/chaos-charts/$faultCommit/faults/kubernetes/container-kill/fault.yaml",
        $containerKillManifest
    )
)

foreach ($download in $downloads) {
    if (-not (Test-Path -LiteralPath $download[1])) {
        Invoke-WebRequest -Uri $download[0] -OutFile $download[1]
    }
}

$operatorContent = Get-Content -LiteralPath $operatorManifest -Raw
$podDeleteContent = Get-Content -LiteralPath $podDeleteManifest -Raw
$containerKillContent = Get-Content -LiteralPath $containerKillManifest -Raw
if ($operatorContent -notmatch 'name: chaos-operator-ce' -or $operatorContent -notmatch "chaos-operator:$operatorVersion") {
    throw "Downloaded Litmus operator manifest did not match the pinned version."
}
if ($podDeleteContent -notmatch 'kind: ChaosExperiment' -or $podDeleteContent -notmatch 'name: pod-delete') {
    throw "Downloaded Pod Delete manifest is invalid."
}
if ($containerKillContent -notmatch 'kind: ChaosExperiment' -or $containerKillContent -notmatch 'name: container-kill') {
    throw "Downloaded Container Kill manifest is invalid."
}

kubectl --context $Context apply -f $operatorManifest
kubectl --context $Context -n litmus rollout status deployment/chaos-operator-ce --timeout=180s
kubectl --context $Context apply -f $runnerRbacManifest
kubectl --context $Context -n litmus apply -f $podDeleteManifest
kubectl --context $Context -n litmus apply -f $containerKillManifest

kubectl --context $Context -n litmus get pods
kubectl --context $Context -n litmus get chaosexperiments
