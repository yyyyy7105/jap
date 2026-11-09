# Start the restaurant watcher in the background (emails new slots as they appear).
# Same as `jap r start` and the UI's Start button: PID in .state\watch.pid, log in .state\watch.log.
# Stop it with .\stop-watch.ps1
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
uv run --no-sync jap r start @args
exit $LASTEXITCODE
