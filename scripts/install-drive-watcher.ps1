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

# Resolve uv here rather than inside the task: a scheduled task may not inherit
# the interactive PATH, and a failure there would be invisible.  Prefer the
# per-user install over whatever is first on PATH, because PATH may be pointing
# at a tool-bundled copy that belongs to the current shell rather than the
# sign-in session the watcher will actually run in.
$uv = $null
$preferredUv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (Test-Path -LiteralPath $preferredUv) {
    $uv = (Resolve-Path -LiteralPath $preferredUv).Path
}
else {
    $uv = (Get-Command uv -ErrorAction Stop).Source
    Write-Warning "uv not found at $preferredUv; using $uv."
}

$shell = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $shell) {
    $shell = (Get-Command powershell -ErrorAction Stop).Source
}

$runnerArguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -IntervalSec $IntervalSec -UvPath `"$uv`""
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

$log = Join-Path $root 'logs\drive-watcher.log'

# Registering a scheduled task needs elevation on a managed machine, and this
# script previously failed outright there - which is one way the watcher ends up
# never installed and minutes quietly stop.  Fall back to a Startup entry, the
# same way install-dashboard-task.ps1 does, and say which path was taken.
$installedAsTask = $false
try {
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $logonTrigger, $supervisorTrigger `
        -Settings $settings `
        -Description "Continuous Drive watcher; no timed or nightly processing. The repeating trigger only restarts the watcher when it is not running. Log: logs\drive-watcher.log" `
        -Force `
        -ErrorAction Stop | Out-Null
    $installedAsTask = $true
}
catch {
    Write-Warning "Scheduled task registration failed ($($_.Exception.Message.Trim())); installing a Startup entry instead."
}

if ($installedAsTask) {
    Write-Host "Installed scheduled task '$taskName'."
    Write-Host "  Polls Drive every $IntervalSec seconds; starts at sign-in."
    Write-Host "  Restarted within $SupervisorMinutes minutes if it stops."
    Write-Host "  Log: $log"

    if ($StartNow) {
        Start-ScheduledTask -TaskName $taskName
        Write-Host 'Started the Drive watcher now.'
    }
}
else {
    $shortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'Meeting Memory Drive Watcher.lnk'
    $wscript = New-Object -ComObject WScript.Shell
    $shortcut = $wscript.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $shell
    $shortcut.Arguments = $runnerArguments
    $shortcut.WorkingDirectory = $root
    $shortcut.WindowStyle = 7
    $shortcut.Description = 'Continuous Drive watcher for Meeting Memory.'
    $shortcut.Save()

    Write-Host "Installed Startup entry: $shortcutPath"
    Write-Host "  Polls Drive every $IntervalSec seconds; starts at sign-in."
    Write-Host '  The runner restarts the watcher itself if it stops.'
    Write-Host "  Log: $log"
    Write-Host 'Run this script from an elevated shell for OS-level supervision instead.'

    if ($StartNow) {
        Start-Process -FilePath $shell -ArgumentList $runnerArguments -WorkingDirectory $root -WindowStyle Hidden
        Write-Host 'Started the Drive watcher now.'
    }
}

if ($StartNow -and -not $CatchUp) {
    Write-Host 'Recordings already staged locally are left alone; re-run with -CatchUp to process them.'
}
