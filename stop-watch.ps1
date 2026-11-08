# Stop the background watcher started by .\start-watch.ps1 and remove its saved PID.
$ErrorActionPreference = "Stop"
$pidFile = Join-Path $PSScriptRoot ".state\watch.pid"

if (-not (Test-Path $pidFile)) {
    Write-Host "Not running (no .state\watch.pid)."
    exit 0
}

$id = (Get-Content $pidFile -Raw).Trim()
if ($id -and (Get-Process -Id $id -ErrorAction SilentlyContinue)) {
    # /T also stops the python process uv started underneath
    cmd /c "taskkill /PID $id /T /F >nul 2>&1"
    Start-Sleep -Milliseconds 500
    if (Get-Process -Id $id -ErrorAction SilentlyContinue) {
        Write-Host "Could not stop PID $id (access denied?). If it was started from an Administrator window, run this from one too."
        exit 1   # keep the PID file: it's still running
    }
    Write-Host "Watcher stopped (PID $id)."
} else {
    Write-Host "PID $id was not running."
}
Remove-Item $pidFile
