# Start the restaurant watcher in the background (emails new slots as they appear).
# Keeps running after this window closes. PID goes to .state\watch.pid, output to .state\watch.log.
# Stop it with .\stop-watch.ps1
param([string[]]$ExtraArgs = @())

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$state = Join-Path $PSScriptRoot ".state"
$pidFile = Join-Path $state "watch.pid"
$log = Join-Path $state "watch.log"
$errLog = Join-Path $state "watch.err.log"
New-Item -ItemType Directory -Force $state | Out-Null

if (Test-Path $pidFile) {
    $old = (Get-Content $pidFile -Raw).Trim()
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Write-Host "Already running (PID $old). Stop it first with .\stop-watch.ps1"
        exit 1
    }
    Remove-Item $pidFile  # stale: that process is gone
}

$env:PYTHONIOENCODING = "utf-8"   # Japanese restaurant names in the log
$env:PYTHONUNBUFFERED = "1"       # write the log as it happens
$env:COLUMNS = "160"              # wide tables in the log

$argList = @("run", "jap", "r", "watch", "--email") + $ExtraArgs
$p = Start-Process -FilePath "uv" -ArgumentList $argList -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $errLog -PassThru

Set-Content -Path $pidFile -Value $p.Id -Encoding ascii
Write-Host "Watcher started (PID $($p.Id)). Log: $log"
