#Requires -Version 5.1
<#
.SYNOPSIS
    Verify an on-demand Meeting Minutes Compiler installation.

.DESCRIPTION
    Runs non-destructive checks only. It verifies the remote transcription
    configuration, Drive capture authorization, graph storage, and dashboard.
    It never creates a scheduler and never prints secret values.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot

$results = [System.Collections.ArrayList]::new()

function Add-Result {
    param([string]$Check, [bool]$Pass, [string]$Detail, [string]$Fix = "")
    [void]$results.Add([PSCustomObject]@{
        Status = if ($Pass) { "ok" } else { "fail" }
        Check = $Check
        Detail = $Detail
        Fix = $Fix
    })
}

try {
    Write-Host ""
    Write-Host "Meeting Minutes Compiler - Continuous Drive Verification" -ForegroundColor Cyan
    Write-Host ""

    $checks = @(
        @{ Name = "pipeline doctor"; Command = { uv run pipeline doctor }; Fix = "uv run pipeline doctor" }
        @{ Name = "Drive capture"; Command = { uv run pipeline capture --dry-run }; Fix = "uv run pipeline auth-drive" }
    )

    foreach ($check in $checks) {
        Write-Host "  $($check.Name)..." -NoNewline
        & $check.Command 2>&1 | Out-Null
        $pass = $LASTEXITCODE -eq 0
        Add-Result $check.Name $pass $(if ($pass) { "ok" } else { "failed" }) $check.Fix
        Write-Host $(if ($pass) { " ok" } else { " failed" }) -ForegroundColor $(if ($pass) { "Green" } else { "Yellow" })
    }

    Write-Host "  Postgres..." -NoNewline
    docker exec mmc-postgres pg_isready -U lightrag -d rag 2>&1 | Out-Null
    $postgresOk = $LASTEXITCODE -eq 0
    Add-Result "Postgres" $postgresOk $(if ($postgresOk) { "accepting connections" } else { "not responding" }) "docker compose up -d"
    Write-Host $(if ($postgresOk) { " ok" } else { " failed" }) -ForegroundColor $(if ($postgresOk) { "Green" } else { "Yellow" })

    Write-Host "  Dashboard..." -NoNewline
    try {
        $dashboard = Invoke-WebRequest -Uri "http://127.0.0.1:8765" -UseBasicParsing -TimeoutSec 5
        $dashboardOk = $dashboard.StatusCode -eq 200
    } catch {
        $dashboardOk = $false
    }
    Add-Result "Dashboard" $dashboardOk $(if ($dashboardOk) { "responding on :8765" } else { "not running" }) "open-dashboard.ps1 -Port 8765"
    Write-Host $(if ($dashboardOk) { " ok" } else { " not running" }) -ForegroundColor $(if ($dashboardOk) { "Green" } else { "Yellow" })

    Write-Host ""
    $results | Format-Table -AutoSize | Out-String | Write-Host
    if (@($results | Where-Object Status -eq "fail").Count) { exit 1 }
} finally {
    Pop-Location
}
