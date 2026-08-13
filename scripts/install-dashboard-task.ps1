[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [string]$TaskName = "Meeting Minutes Compiler - Dashboard"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$launcher = Join-Path $PSScriptRoot "open-dashboard.ps1"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment not found. Run 'uv sync' in $projectRoot first."
}

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m pipeline.cli dashboard --host 127.0.0.1 --port $Port" `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -User $currentUser `
        -RunLevel Limited `
        -Force `
        -ErrorAction Stop | Out-Null

    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Write-Host "Installed and started '$TaskName'. It opens Meeting Memory at http://127.0.0.1:$Port after every sign-in."
}
catch {
    $startup = [Environment]::GetFolderPath("Startup")
    $shortcutPath = Join-Path $startup "Meeting Memory Dashboard.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $PSHOME "pwsh.exe"
    $shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`" -Port $Port"
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.WindowStyle = 7
    $shortcut.Save()

    & $launcher -Port $Port
    Write-Host "Windows blocked Scheduled Tasks, so a Startup shortcut was installed instead: $shortcutPath"
}
