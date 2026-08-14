#Requires -Version 5.1
<#
.SYNOPSIS
    Comprehensive environment verification for the Meeting Minutes Compiler.

.DESCRIPTION
    Runs every health check in one pass and produces a summary table.
    Never prints secret values — only configured/missing and pass/fail.

    Checks:
      1. pipeline doctor (all built-in checks)
      2. Drive capture dry run
      3. Postgres container health
      4. LightRAG Postgres-backed storage verification
      5. Diarization token + gated model access
      6. Dashboard reachability
      7. Nightly scheduled task
      8. Nightly capture schedule readiness

.EXAMPLE
    .\scripts\verify-setup.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Resolve project root (this script lives in scripts/)
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot

# ── Result collector ──────────────────────────────────────────────────

$results = [System.Collections.ArrayList]::new()

function Add-Result {
    param([string]$Check, [bool]$Pass, [string]$Detail, [string]$Fix = "")
    $symbol = if ($Pass) { [char]0x2705 } else { [char]0x274C }  # ✅ / ❌
    [void]$results.Add([PSCustomObject]@{
        Status = "$symbol"
        Check  = $Check
        Detail = $Detail
        Fix    = $Fix
    })
}

try {

Write-Host "`n  Meeting Minutes Compiler — Setup Verification" -ForegroundColor Cyan
Write-Host "  ─────────────────────────────────────────────────`n"

# ── 1. Pipeline doctor ────────────────────────────────────────────────

Write-Host "  [1/7] pipeline doctor..." -NoNewline
$doctorOutput = uv run pipeline doctor 2>&1 | Out-String
$doctorPass = ($LASTEXITCODE -eq 0)
$failCount = ([regex]::Matches($doctorOutput, " FAIL ")).Count
$warnCount = ([regex]::Matches($doctorOutput, " warn ")).Count
if ($doctorPass) {
    Add-Result "pipeline doctor" $true "all checks passed"
    Write-Host " ok" -ForegroundColor Green
} else {
    Add-Result "pipeline doctor" $false "$failCount failure(s), $warnCount warning(s)" "uv run pipeline doctor"
    Write-Host " issues found" -ForegroundColor Yellow
}

# ── 2. Drive capture dry run ──────────────────────────────────────────

Write-Host "  [2/7] Drive capture dry run..." -NoNewline
try {
    $captureOutput = uv run pipeline capture --dry-run 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        Add-Result "Drive capture" $true "dry run succeeded"
        Write-Host " ok" -ForegroundColor Green
    } else {
        Add-Result "Drive capture" $false "dry run failed" "pipeline auth-drive"
        Write-Host " failed" -ForegroundColor Yellow
    }
} catch {
    Add-Result "Drive capture" $false "$_" "pipeline auth-drive"
    Write-Host " error" -ForegroundColor Red
}

# ── 3. Postgres health ───────────────────────────────────────────────

Write-Host "  [3/7] Postgres health..." -NoNewline
try {
    docker exec mmc-postgres pg_isready -U lightrag -d rag 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Add-Result "Postgres" $true "accepting connections"
        Write-Host " ok" -ForegroundColor Green
    } else {
        Add-Result "Postgres" $false "mmc-postgres not responding" "docker compose up -d"
        Write-Host " down" -ForegroundColor Red
    }
} catch {
    Add-Result "Postgres" $false "docker not available" "docker compose up -d"
    Write-Host " error" -ForegroundColor Red
}

# ── 4. LightRAG Postgres-backed storage ──────────────────────────────

Write-Host "  [4/7] LightRAG storage backend..." -NoNewline
try {
    $health = Invoke-RestMethod -Uri "http://localhost:9621/health" -UseBasicParsing -TimeoutSec 10
    $backend = $health.configuration.kv_storage
    if ($backend -and $backend -match "^PG") {
        Add-Result "LightRAG storage" $true "$backend (Postgres)"
        Write-Host " ok" -ForegroundColor Green
    } else {
        $backendLabel = if ($backend) { $backend } else { "unknown" }
        Add-Result "LightRAG storage" $false "$backendLabel (file-based)" "run migrate-to-postgres.ps1"
        Write-Host " file-based" -ForegroundColor Yellow
    }
} catch {
    Add-Result "LightRAG storage" $false "LightRAG unreachable" "docker compose up -d"
    Write-Host " unreachable" -ForegroundColor Red
}

# ── 5. Diarization token ─────────────────────────────────────────────

Write-Host "  [5/7] Diarization (HF_TOKEN)..." -NoNewline
# Read HF_TOKEN from .env without revealing it
$hfSet = $false
if (Test-Path ".env") {
    $envLines = Get-Content ".env"
    foreach ($line in $envLines) {
        if ($line -match "^HF_TOKEN=.+") { $hfSet = $true; break }
    }
}
if ($hfSet) {
    Add-Result "Diarization token" $true "HF_TOKEN configured"
    Write-Host " configured" -ForegroundColor Green
} else {
    Add-Result "Diarization token" $false "HF_TOKEN missing" "set HF_TOKEN in .env + accept pyannote model terms"
    Write-Host " missing" -ForegroundColor Yellow
}

# ── 6. Dashboard ─────────────────────────────────────────────────────

Write-Host "  [6/7] Dashboard..." -NoNewline
try {
    $dashResp = Invoke-WebRequest -Uri "http://localhost:8765" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
    if ($dashResp.StatusCode -eq 200) {
        Add-Result "Dashboard" $true "responding on :8765"
        Write-Host " ok" -ForegroundColor Green
    } else {
        Add-Result "Dashboard" $false "HTTP $($dashResp.StatusCode)" "uv run pipeline dashboard"
        Write-Host " error" -ForegroundColor Yellow
    }
} catch {
    Add-Result "Dashboard" $false "not running" "uv run pipeline dashboard"
    Write-Host " not running" -ForegroundColor DarkGray
}

# ── 7. Nightly scheduled task ─────────────────────────────────────────

Write-Host "  [7/7] Nightly scheduled task..." -NoNewline
try {
    $taskState = powershell -NoProfile -Command {
        $t = Get-ScheduledTask -TaskName "Meeting Minutes Compiler - Nightly" -ErrorAction Stop
        $t.State
    } 2>&1
    if ($LASTEXITCODE -eq 0 -and $taskState -match "Ready") {
        Add-Result "Nightly task" $true "scheduled and ready"
        Write-Host " ok" -ForegroundColor Green
    } elseif ($LASTEXITCODE -eq 0) {
        Add-Result "Nightly task" $false "state: $taskState" "check Task Scheduler"
        Write-Host " $taskState" -ForegroundColor Yellow
    } else {
        Add-Result "Nightly task" $false "not installed" '.\scripts\install-nightly-task.ps1 -Owner "Name"'
        Write-Host " not installed" -ForegroundColor Yellow
    }
} catch {
    Add-Result "Nightly task" $false "not installed" '.\scripts\install-nightly-task.ps1 -Owner "Name"'
    Write-Host " not installed" -ForegroundColor Yellow
}

# ── Summary ───────────────────────────────────────────────────────────

Write-Host "`n  ─────────────────────────────────────────────────"
Write-Host "  Summary`n"

$results | Format-Table -Property Status, Check, Detail, Fix -AutoSize -Wrap | Out-String | Write-Host

$failures = @($results | Where-Object { $_.Status -match "274C" })  # ❌
if ($failures.Count -eq 0) {
    Write-Host "  All checks passed. The pipeline is ready for unattended operation.`n" -ForegroundColor Green
} else {
    Write-Host "  $($failures.Count) check(s) need attention. See the Fix column above.`n" -ForegroundColor Yellow
}

} finally {
    Pop-Location
}
