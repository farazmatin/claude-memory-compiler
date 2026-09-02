<#
.SYNOPSIS
    Runs the continuous Drive watcher with its output captured to a log.

.DESCRIPTION
    Scheduled Tasks discard a task's stdout/stderr, so a watcher that dies -
    Replicate outage, expired Drive authorization, an unhandled crash - stops
    generating minutes with no trace of why.  This wrapper keeps the watcher's
    own per-cycle reporting in logs/drive-watcher.log so a stalled pipeline is
    diagnosable after the fact instead of only noticeable as missing minutes.

    This is still the on-demand watcher, not a timed batch: it processes a
    recording when Drive capture stages a new one.
#>
[CmdletBinding()]
param(
    [ValidateRange(15, 3600)]
    [int]$IntervalSec = 60,

    [switch]$CatchUp,

    # Resolved at install time and passed in, because a scheduled task does not
    # necessarily inherit the interactive PATH that puts uv on it.
    [string]$UvPath,

    [ValidateRange(1, 1024)]
    [int]$MaxLogMegabytes = 5,

    # Exit when the watcher exits instead of restarting it. For debugging.
    [switch]$NoRestart,

    [ValidateRange(5, 3600)]
    [int]$RestartDelaySec = 30,

    [ValidateRange(30, 86400)]
    [int]$MaxRestartDelaySec = 900,

    # A run lasting at least this long counts as healthy, so its restart delay
    # resets instead of continuing to back off.
    [ValidateRange(30, 86400)]
    [int]$HealthyRunSec = 300
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$logDirectory = Join-Path $projectRoot "logs"
$log = Join-Path $logDirectory "drive-watcher.log"

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

# Rotate on start rather than on a size check per line: the watcher emits one
# line per poll, so the only moment the log can jump a tier is a fresh start.
if (Test-Path -LiteralPath $log) {
    $existing = Get-Item -LiteralPath $log
    if ($existing.Length -gt ($MaxLogMegabytes * 1MB)) {
        Move-Item -LiteralPath $log -Destination "$log.1" -Force
    }
}

function Write-WatcherLog {
    param([string]$Message)
    "[{0}] {1}" -f [DateTimeOffset]::Now.ToString("yyyy-MM-dd HH:mm:ssK"), $Message |
        Add-Content -LiteralPath $log -Encoding utf8
}

if ($UvPath -and (Test-Path -LiteralPath $UvPath)) {
    $uv = $UvPath
}
else {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $command) {
        Write-WatcherLog "uv not found on PATH and -UvPath '$UvPath' does not exist; cannot start."
        exit 1
    }
    $uv = $command.Source
}
$arguments = @("run", "pipeline", "watch", "--interval-sec", $IntervalSec)
if ($CatchUp) {
    $arguments += "--catch-up"
}

Set-Location -LiteralPath $projectRoot
Write-WatcherLog "runner starting; interval ${IntervalSec}s; catch-up $([bool]$CatchUp); restart $(-not $NoRestart)"

# Registering a scheduled task requires elevation on some machines, so this
# runner is also launched from the Startup folder, where nothing supervises it.
# Restart the watcher here rather than leaving a crash to stop minutes until the
# next sign-in.  Backoff grows only for failures that die quickly, so a genuine
# outage does not spin, while a watcher that ran for a while restarts promptly.
$backoffSec = $RestartDelaySec
$firstRun = $true

while ($true) {
    if (-not $firstRun) {
        Write-WatcherLog "restarting watcher in ${backoffSec}s"
        Start-Sleep -Seconds $backoffSec
    }
    $firstRun = $false

    $startedAt = Get-Date
    $alreadyRunning = $false

    & $uv @arguments 2>&1 | ForEach-Object {
        $line = [string]$_
        if ($line -match 'already running') {
            $alreadyRunning = $true
        }
        Write-WatcherLog $line
    }
    $exitCode = $LASTEXITCODE
    $ranForSec = [int]((Get-Date) - $startedAt).TotalSeconds

    Write-WatcherLog "watcher exited with code $exitCode after ${ranForSec}s"

    # Another watcher holds the single-flight lease. Restarting would just lose
    # the race again every cycle, so leave the running one alone.
    if ($alreadyRunning) {
        Write-WatcherLog "another watcher already holds the lease; this runner is exiting."
        exit 0
    }

    if ($NoRestart) {
        exit $exitCode
    }

    if ($ranForSec -ge $HealthyRunSec) {
        $backoffSec = $RestartDelaySec
    }
    else {
        $backoffSec = [Math]::Min($backoffSec * 2, $MaxRestartDelaySec)
    }
}
