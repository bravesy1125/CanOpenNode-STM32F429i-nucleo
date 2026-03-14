param(
    [switch]$Mock = $false,
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 80
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$frontendDir = Join-Path $PSScriptRoot "frontend"
$backendDir = Join-Path $PSScriptRoot "backend"
$nodeDir = "C:\Program Files\nodejs"
$npmCmd = Join-Path $nodeDir "npm.cmd"

if (-not (Test-Path $npmCmd)) {
    throw "Node.js not found at $npmCmd"
}

$env:Path = "$nodeDir;$env:Path"

Write-Host "Building frontend..."
Push-Location $frontendDir
try {
    & $npmCmd run build
}
finally {
    Pop-Location
}

if ($Mock) {
    $env:CANOPEN_MOCK = "1"
}
else {
    Remove-Item Env:CANOPEN_MOCK -ErrorAction SilentlyContinue
}

Write-Host "Starting uvicorn on http://$BindHost`:$Port ..."
Push-Location $backendDir
try {
    python -m uvicorn app.main:app --host $BindHost --port $Port
}
finally {
    Pop-Location
}
