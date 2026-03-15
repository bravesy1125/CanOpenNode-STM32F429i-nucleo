param(
    [switch]$Mock = $false,
    [string]$BindHost = "0.0.0.0",
    [int]$Port = 80
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$frontendDir = Join-Path $PSScriptRoot "frontend"
$backendDir = Join-Path $PSScriptRoot "backend"
$nodeDir = "C:\Program Files\nodejs"
$npmCmd = Join-Path $nodeDir "npm.cmd"

function Stop-PortListeners {
    param(
        [int]$TargetPort
    )

    $listenLines = cmd /c "netstat -ano | findstr :$TargetPort | findstr LISTENING"
    if (-not $listenLines) {
        return
    }

    $pids = @()
    foreach ($line in $listenLines) {
        $parts = ($line -split "\s+") | Where-Object { $_ }
        if ($parts.Count -gt 0) {
            $pidText = $parts[-1]
            if ($pidText -match '^\d+$') {
                $pids += [int]$pidText
            }
        }
    }

    $pids = $pids | Sort-Object -Unique
    foreach ($listenerPid in $pids) {
        try {
            $process = Get-Process -Id $listenerPid -ErrorAction Stop
            Write-Host "Stopping process on port ${TargetPort}: PID=$listenerPid Name=$($process.ProcessName)"
            Stop-Process -Id $listenerPid -Force -ErrorAction Stop
        }
        catch {
            Write-Warning "Failed to stop PID $listenerPid on port ${TargetPort}: $($_.Exception.Message)"
        }
    }

    Start-Sleep -Milliseconds 800
}

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

Stop-PortListeners -TargetPort $Port

Write-Host "Starting uvicorn on http://$BindHost`:$Port ..."
Push-Location $backendDir
try {
    python -m uvicorn app.main:app --host $BindHost --port $Port
}
finally {
    Pop-Location
}
