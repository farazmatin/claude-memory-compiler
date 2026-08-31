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

    [ValidateRange(1, 1024)]
    [int]$MaxLogMegabytes = 5
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

$uv = (Get-Command uv -ErrorAction Stop).Source
$arguments = @("run", "pipeline", "watch", "--interval-sec", $IntervalSec)
if ($CatchUp) {
    $arguments += "--catch-up"
}

Set-Location -LiteralPath $projectRoot
Write-WatcherLog "watcher starting; interval ${IntervalSec}s; catch-up $([bool]$CatchUp)"

& $uv @arguments 2>&1 | ForEach-Object { Write-WatcherLog $_ }
$exitCode = $LASTEXITCODE

Write-WatcherLog "watcher exited with code $exitCode"
exit $exitCode
