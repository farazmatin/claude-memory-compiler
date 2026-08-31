<#
.SYNOPSIS
    Installs the continuous Drive watcher as a scheduled task.

.DESCRIPTION
    The watcher is the sanctioned automation: it polls the approved private
    Drive folders and runs the normal pipeline when capture stages a newly
    arrived recording.  It is not a nightly batch.

    Three settings exist so a watcher that stops does not silently stop minutes
    generation, which is how this has failed before:

      * No execution time limit.  Scheduled Tasks default to stopping an action
        after 72 hours, which terminates a healthy continuous watcher after
        three days.
      * Restart on failure, plus a short repeating trigger that re-starts the
        task only when no instance is running (MultipleInstances IgnoreNew makes
        it a no-op while the watcher is alive).  This supervises the watcher; it
        does not batch-process anything.
      * Output captured through run-drive-watcher.ps1 into
        logs/drive-watcher.log, because a task's stdout is otherwise discarded.
#>
param(
    [ValidateRange(15, 3600)]
    [int]$IntervalSec = 60,

    [ValidateRange(1, 1440)]
    [int]$SupervisorMinutes = 15,

    [switch]$StartNow,

    # Also process recordings already pending locally when the watcher starts.
    # Off by default so installing the watcher cannot silently begin paid
    # transcription of previously staged recordings.
    [switch]$CatchUp
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot 'run-drive-watcher.ps1'
$taskName = 'Meeting Memory - Drive Watcher'

if (-not (Test-Path -LiteralPath $runner)) {
    throw "Watcher runner not found at $runner."
}

# Fail here rather than inside the task, where the error would be invisible.
$null = Get-Command uv -ErrorAction Stop

$shell = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $shell) {
    $shell = (Get-Command powershell -ErrorAction Stop).Source
}

$runnerArguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -IntervalSec $IntervalSec"
if ($CatchUp) {
    $runnerArguments += ' -CatchUp'
}

$action = New-ScheduledTaskAction -Execute $shell -Argument $runnerArguments -WorkingDirectory $root

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn

# First supervision lands one full interval out, so installing the watcher does
# not itself start it. Use -StartNow to begin immediately and keep the decision
# to spend on transcription with the operator.
$supervisorTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes($SupervisorMinutes) `
    -RepetitionInterval (New-TimeSpan -Minutes $SupervisorMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $logonTrigger, $supervisorTrigger `
    -Settings $settings `
    -Description "Continuous Drive watcher; no timed or nightly processing. The repeating trigger only restarts the watcher when it is not running. Log: logs\drive-watcher.log" `
    -Force | Out-Null

Write-Host "Installed $taskName."
Write-Host "  Polls Drive every $IntervalSec seconds; starts at sign-in."
Write-Host "  Restarted within $SupervisorMinutes minutes if it stops."
Write-Host "  Log: $(Join-Path $root 'logs\drive-watcher.log')"

if ($StartNow) {
    Start-ScheduledTask -TaskName $taskName
    Write-Host 'Started the Drive watcher now.'
    if (-not $CatchUp) {
        Write-Host 'Recordings already staged locally are left alone; re-run with -CatchUp to process them.'
    }
}
