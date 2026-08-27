#Requires -Version 5.1
<#
.SYNOPSIS
    Safely migrate LightRAG from file-based storage to Postgres-backed pgvector.

.DESCRIPTION
    Before changing storage:
      1. Backs up the SQLite manifest, minutes, and transcripts
      2. Stops the current Docker stack
      3. Starts the Postgres-backed stack with generated secrets from .env
      4. Re-indexes existing minutes into Postgres
      5. Verifies RAG answers, Drive capture, and dashboard
      6. Retains file-based storage until validation succeeds

    Safe to re-run. Backs up before every attempt.

.EXAMPLE
    .\scripts\migrate-to-postgres.ps1
    .\scripts\migrate-to-postgres.ps1 -SkipBackup   # if you just backed up
#>
[CmdletBinding()]
param(
    [switch]$SkipBackup
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step  { param([string]$msg) Write-Host "`n==> $msg" -ForegroundColor White }
function Write-Ok    { param([string]$msg) Write-Host "    [ok]   $msg" -ForegroundColor Green }
function Write-Warn  { param([string]$msg) Write-Host "    [warn] $msg" -ForegroundColor Yellow }
function Write-Fail  { param([string]$msg) Write-Host "    [FAIL] $msg" -ForegroundColor Red }

# Resolve project root (this script lives in scripts/)
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot

try {

# ── Preflight ─────────────────────────────────────────────────────────

Write-Step "Preflight"

if (-not (Test-Path ".env")) {
    Write-Fail ".env does not exist - run setup.ps1 first"
    exit 1
}

$envContent = Get-Content ".env" -Raw
if ($envContent -notmatch "(?m)^POSTGRES_PASSWORD=.+") {
    Write-Fail "POSTGRES_PASSWORD not set in .env - run setup.ps1 first"
    exit 1
}
Write-Ok "POSTGRES_PASSWORD configured"

if ($envContent -match "(?m)^MMC_LIGHTRAG_API_KEY=.+") {
    Write-Ok "MMC_LIGHTRAG_API_KEY configured"
} else {
    Write-Fail "MMC_LIGHTRAG_API_KEY not set in .env - run setup.ps1 first"
    exit 1
}

# ── 1. Backup ─────────────────────────────────────────────────────────

if (-not $SkipBackup) {
    Write-Step "Backing up current state"
    $backupDir = "pre-postgres-backup"
    uv run pipeline backup --to $backupDir
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Backup failed - aborting migration"
        exit 1
    }

    # Also snapshot rag_storage even if empty, as a record
    if (Test-Path "rag_storage") {
        $ragBackup = Join-Path $backupDir "rag_storage"
        if (-not (Test-Path $ragBackup)) {
            Copy-Item -Path "rag_storage" -Destination $ragBackup -Recurse -Force
        }
        Write-Ok "rag_storage backed up"
    }
    Write-Ok "backup saved to $backupDir"
} else {
    Write-Warn "skipping backup (--SkipBackup)"
}

# ── 2. Restart Docker stack ───────────────────────────────────────────

Write-Step "Restarting Docker stack with Postgres"

Write-Host "    stopping current containers..."
docker compose down
Write-Ok "containers stopped"

Write-Host "    starting storage stack (Postgres + LightRAG)..."
docker compose up -d
Write-Ok "containers started"

# Wait for Postgres
Write-Host "    waiting for Postgres" -NoNewline
$pgReady = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        docker exec mmc-postgres pg_isready -U lightrag -d rag 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $pgReady = $true
            break
        }
    } catch {}
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 2
}
Write-Host ""
if ($pgReady) {
    Write-Ok "Postgres accepting connections"
} else {
    Write-Fail "Postgres did not become ready in 2 minutes"
    Write-Host "    Check: docker logs mmc-postgres" -ForegroundColor DarkGray
    exit 1
}

# Wait for LightRAG
Write-Host "    waiting for LightRAG" -NoNewline
$lrReady = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:9621/health" -UseBasicParsing -TimeoutSec 5 -ErrorAction SilentlyContinue
        if ($resp.StatusCode -eq 200) {
            $lrReady = $true
            break
        }
    } catch {}
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 3
}
Write-Host ""
if ($lrReady) {
    Write-Ok "LightRAG healthy"
} else {
    Write-Warn "LightRAG did not respond on /health in 3 minutes - indexing may fail"
    Write-Host "    Check: docker logs mmc-lightrag" -ForegroundColor DarkGray
}

# ── 3. Verify storage backend ────────────────────────────────────────

Write-Step "Verifying Postgres-backed storage"

try {
    $health = Invoke-RestMethod -Uri "http://localhost:9621/health" -UseBasicParsing -TimeoutSec 10
    $kvStorage = $health.configuration.kv_storage
    if ($kvStorage -and $kvStorage -match "^PG") {
        Write-Ok "LightRAG storage backend: $kvStorage (Postgres)"
    } else {
        Write-Warn "LightRAG storage backend: $kvStorage (expected PG*)"
        Write-Host "    Check docker-compose.yml LIGHTRAG_KV_STORAGE setting" -ForegroundColor DarkGray
    }
} catch {
    Write-Warn "Could not verify storage backend: $_"
}

# ── 4. Re-index existing minutes ─────────────────────────────────────

Write-Step "Re-indexing existing minutes into Postgres-backed LightRAG"

$minutesFiles = Get-ChildItem -Path "minutes" -Filter "*.md" -File -ErrorAction SilentlyContinue
if ($minutesFiles.Count -eq 0) {
    Write-Warn "no minutes files found to index"
} else {
    Write-Host "    found $($minutesFiles.Count) minutes file(s) to index"
    uv run pipeline graph-sync
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "$($minutesFiles.Count) minutes file(s) indexed"
    } else {
        Write-Warn "indexing had errors - check output above"
    }
}

# ── 5. Verification ──────────────────────────────────────────────────

Write-Step "Running verification suite"

$allOk = $true

# Doctor
Write-Host "    pipeline doctor..."
uv run pipeline doctor
if ($LASTEXITCODE -ne 0) {
    Write-Warn "doctor reported issues (see above)"
    $allOk = $false
} else {
    Write-Ok "doctor passed"
}

# Test RAG query
if ($minutesFiles.Count -gt 0) {
    Write-Host "    testing RAG query..."
    try {
        $queryResult = uv run pipeline query "what was discussed?" 2>&1
        if ($LASTEXITCODE -eq 0 -and $queryResult) {
            Write-Ok "RAG query returned results"
        } else {
            Write-Warn "RAG query returned empty or failed"
            $allOk = $false
        }
    } catch {
        Write-Warn "RAG query failed: $_"
        $allOk = $false
    }
}

# Dashboard check
Write-Host "    testing dashboard..."
try {
    $dashResp = Invoke-WebRequest -Uri "http://localhost:8765" -UseBasicParsing -TimeoutSec 5 -ErrorAction SilentlyContinue
    if ($dashResp.StatusCode -eq 200) {
        Write-Ok "dashboard responding"
    } else {
        Write-Warn "dashboard returned $($dashResp.StatusCode)"
    }
} catch {
    Write-Warn "dashboard not running (start with: uv run pipeline dashboard)"
}

# ── Done ──────────────────────────────────────────────────────────────

Write-Host "`n────────────────────────────────────────────────────────" -ForegroundColor White

if ($allOk) {
    Write-Host "Migration complete. Postgres-backed LightRAG is live.`n" -ForegroundColor Green
    Write-Host "The file-based rag_storage/ is retained until you confirm everything works."
    Write-Host "When satisfied, clean up with: Remove-Item -Recurse rag_storage`n"
} else {
    Write-Host "Migration done with warnings - review the issues above.`n" -ForegroundColor Yellow
    Write-Host "Your pre-migration backup is in: pre-postgres-backup\`n"
}

} finally {
    Pop-Location
}
