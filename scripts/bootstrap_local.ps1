param(
    [string]$ClusterName = "codereferee"
)

$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$localKind = Join-Path $root ".runtime/tools/kind.exe"
if (-not (Get-Command kind -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $localKind)) {
    & (Join-Path $PSScriptRoot "install_kind.ps1")
}

. (Join-Path $PSScriptRoot "bootstrap_kind.ps1") -ClusterName $ClusterName
& (Join-Path $PSScriptRoot "bootstrap_litmus.ps1") -Context $env:CODEREFEREE_KUBECTL_CONTEXT

Write-Host "CodeReferee local Sandbox runtime is ready."
