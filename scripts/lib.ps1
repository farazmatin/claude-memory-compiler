#Requires -Version 5.1
<#
    Shared plumbing for the Windows scripts. Dot-source it:

        . (Join-Path $PSScriptRoot "lib.ps1")

    Nothing here prints a secret. Configuration questions are answered by asking
    the pipeline ("is this key configured?"), never by parsing .env in PowerShell:
    a second parser drifts from the Python one, and the drift shows up as setup
    insisting that a configured value is missing.
#>

# PowerShell 7.3+ turns a non-zero exit from a native command into a terminating
# error when ErrorActionPreference is Stop. `pipeline doctor` exits 1 by design
# when a check fails, and reporting that is the entire job of these scripts.
# Exit codes are inspected explicitly instead.
$PSNativeCommandUseErrorActionPreference = $false

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Problems = New-Object System.Collections.Generic.List[string]

function Write-Step { param([string]$Text) Write-Host "`n==> $Text" -ForegroundColor White }
function Write-Ok { param([string]$Text) Write-Host "    [ok] $Text" -ForegroundColor Green }
function Write-Note { param([string]$Text) Write-Host "    [--] $Text" -ForegroundColor Yellow }
function Write-Bad { param([string]$Text) Write-Host "    [!!] $Text" -ForegroundColor Red }

function Add-Problem {
    param([string]$Text)
    $Problems.Add($Text) | Out-Null
    Write-Bad $Text
}

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-Pipeline {
    <#
        Run a pipeline command in the project environment, leaving its exit code
        in $LASTEXITCODE rather than throwing.

        Takes an array rather than remaining arguments: an advanced function
        refuses to bind anything that looks like a parameter, so `--dry-run`
        would be rejected before it ever reached the CLI.
    #>
    param([string[]]$PipelineArgs)
    Push-Location $ProjectRoot
    try { & uv run pipeline @PipelineArgs }
    finally { Pop-Location }
}

function Invoke-Compose {
    param([string[]]$ComposeArgs)
    Push-Location $ProjectRoot
    try { & docker compose @ComposeArgs }
    finally { Pop-Location }
}

function Test-Configured {
    <#
        Is this key set, in .env or in the environment? The answer comes from the
        pipeline's own configuration reader, so the two never disagree.
    #>
    param([Parameter(Mandatory)][string]$Key)
    Invoke-Pipeline @("config", "show", "--key", $Key) | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Get-DoctorReport {
    <#
        `pipeline doctor --json`, parsed. Returns $null when the Python
        environment is not usable at all - which is itself the finding.
    #>
    Push-Location $ProjectRoot
    try { $raw = & uv run pipeline doctor --json 2>$null | Out-String }
    finally { Pop-Location }
    if (-not $raw.Trim()) { return $null }
    try { return $raw | ConvertFrom-Json }
    catch { return $null }
}

function Get-DoctorCheck {
    param(
        [Parameter(Mandatory)]$Report,
        [Parameter(Mandatory)][string]$Name
    )
    return $Report.checks | Where-Object { $_.name -eq $Name } | Select-Object -First 1
}

function Test-DoctorCheck {
    <#
        True only when the named check passed. A missing check is not a pass:
        an older build that does not run it has not verified anything.
    #>
    param(
        [Parameter(Mandatory)]$Report,
        [Parameter(Mandatory)][string]$Name
    )
    $check = Get-DoctorCheck -Report $Report -Name $Name
    return ($null -ne $check -and $check.status -eq "ok")
}

function Write-DoctorReport {
    <#
        Print every check, recording failures as problems. Details come straight
        from `doctor`, which reports configured/missing and never a value.
    #>
    param([Parameter(Mandatory)]$Report)
    foreach ($check in $Report.checks) {
        $line = "{0,-26} {1}" -f $check.name, $check.detail
        if ($check.status -eq "ok") { Write-Ok $line }
        elseif ($check.status -eq "warn") { Write-Note $line }
        else {
            Write-Bad $line
            if ($check.fix) { Write-Host "         -> $($check.fix)" }
            $Problems.Add("$($check.name): $($check.detail)") | Out-Null
        }
    }
}

function Wait-ForDoctorCheck {
    <#
        Poll `doctor` until one check passes. Used while containers come up:
        LightRAG answers /health only after Postgres has accepted it, and that
        gap is minutes on a cold start, not seconds.
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [int]$TimeoutMinutes = 5
    )
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    while ((Get-Date) -lt $deadline) {
        $report = Get-DoctorReport
        if ($report -and (Test-DoctorCheck -Report $report -Name $Name)) { return $true }
        Start-Sleep -Seconds 10
    }
    return $false
}
