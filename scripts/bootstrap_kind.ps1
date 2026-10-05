param(
    [string]$ClusterName = "codereferee"
)

$ErrorActionPreference = "Stop"

if ($ClusterName -notmatch '^[a-z0-9][a-z0-9.-]*$') {
    throw "ClusterName must contain only lower-case letters, numbers, dots, and hyphens."
}

foreach ($command in @("docker", "kubectl")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "$command was not found in PATH."
    }
}

$kindCommand = Get-Command kind -ErrorAction SilentlyContinue
if (-not $kindCommand) {
    $localKind = Join-Path (Split-Path $PSScriptRoot -Parent) ".runtime/tools/kind.exe"
    if (-not (Test-Path -LiteralPath $localKind)) {
        throw "kind was not found. Run ./scripts/install_kind.ps1 first."
    }
    $kindExecutable = $localKind
}
else {
    $kindExecutable = $kindCommand.Source
}

docker info | Out-Null
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$clusterOutput = & $kindExecutable get clusters 2>&1
$clusterExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
$clusters = if ($clusterExitCode -eq 0) { @($clusterOutput) } else { @() }
if ($clusters -notcontains $ClusterName) {
    & $kindExecutable create cluster --name $ClusterName --wait 180s
}

$context = "kind-$ClusterName"
kubectl --context $context get nodes

$env:CODEREFEREE_CLUSTER_PROVIDER = "kind"
$env:CODEREFEREE_KIND_CLUSTER_NAME = $ClusterName
$env:CODEREFEREE_KUBECTL_CONTEXT = $context
$env:CODEREFEREE_KIND_COMMAND = $kindExecutable

Write-Host "kind cluster is ready: $ClusterName"
Write-Host "Current PowerShell process configured with:"
Write-Host "  CODEREFEREE_CLUSTER_PROVIDER=kind"
Write-Host "  CODEREFEREE_KIND_CLUSTER_NAME=$ClusterName"
Write-Host "  CODEREFEREE_KUBECTL_CONTEXT=$context"
Write-Host "  CODEREFEREE_KIND_COMMAND=$kindExecutable"
Write-Host "Dot-source this script to keep these variables in your terminal:"
Write-Host "  . ./scripts/bootstrap_kind.ps1"
