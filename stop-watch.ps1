# Stop the background watcher. Same as `jap r stop` and the UI's Stop button.
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
uv run --no-sync jap r stop
exit $LASTEXITCODE
