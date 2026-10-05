param(
    [string]$Version = "v0.33.0"
)

$ErrorActionPreference = "Stop"

if ($Version -notmatch '^v\d+\.\d+\.\d+$') {
    throw "Version must look like v0.33.0."
}

$root = Split-Path $PSScriptRoot -Parent
$toolDirectory = Join-Path $root ".runtime/tools"
$destination = Join-Path $toolDirectory "kind.exe"
$download = Join-Path $toolDirectory "kind-windows-amd64"
$checksums = Join-Path $toolDirectory "kind-sha256sum.txt"
$baseUrl = "https://github.com/kubernetes-sigs/kind/releases/download/$Version"

New-Item -ItemType Directory -Path $toolDirectory -Force | Out-Null
Invoke-WebRequest -Uri "$baseUrl/kind-windows-amd64" -OutFile $download
Invoke-WebRequest -Uri "$baseUrl/kind-windows-amd64.sha256sum" -OutFile $checksums

$expected = ((Get-Content -LiteralPath $checksums -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
$actual = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) {
    Remove-Item -LiteralPath $download -Force
    throw "kind checksum mismatch: expected $expected, got $actual"
}

Move-Item -LiteralPath $download -Destination $destination -Force
Remove-Item -LiteralPath $checksums -Force
Write-Host "Installed kind $Version at $destination"
