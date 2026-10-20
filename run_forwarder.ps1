# Keeps group_forwarder.py running. The Windows auto-start task runs this (see README, "Start automatically").
# - Restarts the forwarder 30 seconds after it stops, for example after a crash.
# - Stops for good when the forwarder stops within a minute of starting: that is a setup problem
#   (a setting, not logged in, another copy already running) that restarting can't fix.
# - Everything the forwarder prints goes to forwarder.log next to this script.
# The parameters are only there so the tests can use short times; normally run it without any.
param(
    [int]$QuickExitSeconds = 60,
    [int]$RestartDelaySeconds = 30,
    [string]$Python = (Join-Path $PSScriptRoot ".venv\Scripts\python.exe"),
    [string]$Script = (Join-Path $PSScriptRoot "group_forwarder.py"),
    [string]$Log = (Join-Path $PSScriptRoot "forwarder.log")
)

function Write-Log([string]$Text) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Text
    [System.IO.File]::AppendAllText($Log, $line + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding $false))
}

while ($true) {
    Write-Log "run_forwarder.ps1: starting the forwarder"
    $started = Get-Date
    # --no-login: never wait for a phone number in a window nobody sees; --log: append to the log file
    & $Python $Script --no-login --log $Log
    $code = $LASTEXITCODE
    $seconds = [int]((Get-Date) - $started).TotalSeconds
    Write-Log "run_forwarder.ps1: the forwarder stopped (exit code $code) after $seconds seconds"
    if ($seconds -lt $QuickExitSeconds) {
        Write-Log "run_forwarder.ps1: it stopped within $QuickExitSeconds seconds of starting, so restarting won't help. Fix the problem above, then start it again."
        exit 1
    }
    Start-Sleep -Seconds $RestartDelaySeconds
}
