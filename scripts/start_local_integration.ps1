param(
    [int]$BackendPort = 18090,
    [int]$SandboxPort = 8100,
    [int]$FrontendPort = 5173,
    [switch]$DisableLlm
)
$ErrorActionPreference = 'Stop'
$sandbox = Split-Path $PSScriptRoot -Parent
$workspace = Split-Path $sandbox -Parent
$server = Join-Path $workspace 'codereferee-server'
$ai = Join-Path $workspace 'codereferee-AI/ai-core'
$frontend = Join-Path $workspace 'codereferee-frontend'
$runtime = Join-Path $sandbox '.runtime/integration'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
foreach ($port in @($BackendPort, $SandboxPort, $FrontendPort)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use. Stop the previous run or choose a different port."
    }
}
# These are the CodeReferee containers already created from server compose.
# First-time setup: docker compose -f codereferee-server/compose.yml up -d codereferee-db codereferee-redis
docker start codereferee-db codereferee-redis | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Start the CodeReferee DB/Redis containers first.' }
$deadline = (Get-Date).AddSeconds(60)
do {
    $dbHealth = docker inspect --format '{{.State.Health.Status}}' codereferee-db
    $redisHealth = docker inspect --format '{{.State.Health.Status}}' codereferee-redis
    if ($dbHealth -eq 'healthy' -and $redisHealth -eq 'healthy') { break }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
if ($dbHealth -ne 'healthy' -or $redisHealth -ne 'healthy') { throw 'DB/Redis health timeout.' }
# Latest schema contains additive, idempotent ALTER statements for older DBs.
Get-Content -LiteralPath (Join-Path $server 'db/schema.sql') -Raw -Encoding utf8 |
    docker exec -i codereferee-db psql -v ON_ERROR_STOP=1 -U postgres -d codereferee | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Backend schema initialization failed.' }
$env:CODEREFEREE_CLUSTER_PROVIDER = 'kind'
$env:CODEREFEREE_KIND_CLUSTER_NAME = 'codereferee'
$env:CODEREFEREE_KUBECTL_CONTEXT = 'kind-codereferee'
$env:CODEREFEREE_KIND_COMMAND = (Resolve-Path (Join-Path $sandbox '.runtime/tools/kind.exe')).Path
$env:SANDBOX_BASE_URL = "http://127.0.0.1:$SandboxPort"
$env:SANDBOX_HTTP_TIMEOUT_SECONDS = '1800'
$env:CODEREFEREE_BACKEND_URL = "http://127.0.0.1:$BackendPort"
$env:SPRING_DATASOURCE_URL = 'jdbc:postgresql://127.0.0.1:5433/codereferee'
$env:SPRING_DATASOURCE_USERNAME = 'postgres'
$env:SPRING_DATASOURCE_PASSWORD = 'postgres'
$env:SPRING_DATA_REDIS_HOST = '127.0.0.1'
$env:SPRING_DATA_REDIS_PORT = '6379'
$env:PYTHONUNBUFFERED = '1'
if ($DisableLlm) { $env:GOOGLE_API_KEY = ''; $env:LLM_PROVIDER = 'openai-compatible'; $env:LLM_BASE_URL = '' }
foreach ($proxyKey in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')) {
    Remove-Item "Env:$proxyKey" -ErrorAction SilentlyContinue
}
$processes = @()
$processes += Start-Process -FilePath (Join-Path $sandbox '.venv/Scripts/python.exe') -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$SandboxPort") -WorkingDirectory $sandbox -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'sandbox.stdout.log') -RedirectStandardError (Join-Path $runtime 'sandbox.stderr.log')
$processes += Start-Process -FilePath (Join-Path $server 'gradlew.bat') -ArgumentList @('bootRun','--console=plain',"--args=--server.port=$BackendPort") -WorkingDirectory $server -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'server.stdout.log') -RedirectStandardError (Join-Path $runtime 'server.stderr.log')
$processes += Start-Process -FilePath (Join-Path $ai '.venv/Scripts/python.exe') -ArgumentList @('-m','app.worker') -WorkingDirectory $ai -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'ai.stdout.log') -RedirectStandardError (Join-Path $runtime 'ai.stderr.log')
$processes += Start-Process -FilePath 'npm.cmd' -ArgumentList @('run','dev','--','--host','127.0.0.1','--port',"$FrontendPort",'--strictPort') -WorkingDirectory $frontend -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'frontend.stdout.log') -RedirectStandardError (Join-Path $runtime 'frontend.stderr.log')
$processes.Id | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtime 'processes.json') -Encoding utf8
Write-Host "Frontend: http://127.0.0.1:$FrontendPort"
Write-Host "Backend: http://127.0.0.1:$BackendPort"
Write-Host "Sandbox: http://127.0.0.1:$SandboxPort"
Write-Host "Logs: $runtime"
Write-Host 'Services start asynchronously; check logs before submitting a validation.'
