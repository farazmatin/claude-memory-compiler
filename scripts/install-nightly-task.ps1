[CmdletBinding()]
param(
    # Optional. Left unset, the batch takes the owner name from MMC_OWNER_NAME in
    # .env, which keeps it in one place: a name baked into the task argument can
    # only be corrected by re-registering the task, and the two then disagree.
    [string]$Owner,
    [string]$TaskName = "Meeting Minutes Compiler - Nightly"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$uv = (Get-Command uv -ErrorAction Stop).Source
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$argument = "run pipeline run"
if ($Owner) {
    $argument = "$argument --owner `"$Owner`""
}

$action = New-ScheduledTaskAction `
    -Execute $uv `
    -Argument $argument `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At 1:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -User $currentUser `
    -RunLevel Limited `
    -Force | Out-Null

Write-Host "Installed '$TaskName'. It runs at 1:00 AM and skips overlapping runs."
