param(
    [ValidateRange(15, 3600)]
    [int]$IntervalSec = 60,
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$uv = (Get-Command uv -ErrorAction Stop).Source
$taskName = 'Meeting Memory - Drive Watcher'
$action = New-ScheduledTaskAction -Execute $uv -Argument "run pipeline watch --interval-sec $IntervalSec" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'Continuous Drive watcher; no timed or nightly trigger.' -Force | Out-Null
Write-Host "Installed $taskName. It starts at sign-in and polls Drive every $IntervalSec seconds."
if ($StartNow) {
    Start-ScheduledTask -TaskName $taskName
    Write-Host 'Started Drive watcher now. Existing pending recordings are not transcribed unless launched with --catch-up.'
}
