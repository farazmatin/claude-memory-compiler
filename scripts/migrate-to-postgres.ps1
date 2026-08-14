#Requires -Version 5.1
<#
.SYNOPSIS
    Move the LightRAG index onto Postgres without risking the corpus.

.DESCRIPTION
    The index is derived data: every document in it can be rebuilt from
    `minutes/`, which is why this migration copies nothing between stores. It
    backs up what is *not* derived, brings the Postgres-backed stack up, and
    re-inserts the minutes into an empty index.

    Order matters, and the order is the safety:

      1. Back up the manifest, transcripts and minutes first. The manifest is the
         only record of which meeting each transcript belongs to.
      2. Copy rag_storage aside before the new configuration starts, so the old
         file-based index survives as a rollback path.
      3. Refuse to re-index until LightRAG actually reports Postgres storage.
         Re-indexing into the store you are trying to leave achieves nothing and
         costs an hour of CPU-bound extraction.
      4. Verify by asking a real question, not by trusting the exit codes.

    Nothing is deleted. Rollback is putting the old compose configuration back and
    restoring rag_storage from the backup.

        .\scripts\migrate-to-postgres.ps1

.PARAMETER BackupTo
    Backup destination. Defaults to backups\pre-postgres-<timestamp> under the
    project root.

.PARAMETER IncludeAudio
    Back up audio too. Bulky - and Drive already holds the durable copy - so this
    is off by default.

.PARAMETER SkipRagStorageCopy
    Do not copy rag_storage. Only sensible when the index is empty or already on
    Postgres; it gives up the rollback path.

.PARAMETER VerifyQuestion
    The question asked against the migrated index to prove retrieval works.
#>
[CmdletBinding()]
param(
    [string]$BackupTo,
    [switch]$IncludeAudio,
    [switch]$SkipRagStorageCopy,
    [string]$VerifyQuestion = "What decisions were made recently?"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "lib.ps1")

function Stop-Migration {
    param([string]$Reason)
    Write-Host ""
    Write-Bad $Reason
    Write-Host ""
    Write-Host "The index was not modified. Fix the above and re-run." -ForegroundColor Yellow
    exit 1
}

# ── 1. Preconditions ──────────────────────────────────────────────────

Write-Step "Checking preconditions"

if (-not (Test-Command "uv")) { Stop-Migration "uv is not on PATH - run .\scripts\setup.ps1 first" }
if (-not (Test-Command "docker")) { Stop-Migration "docker is not on PATH - run .\scripts\setup.ps1 first" }

& docker info 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Stop-Migration "Docker is not running - start Docker Desktop and re-run" }
Write-Ok "docker running"

foreach ($key in @("MMC_LIGHTRAG_API_KEY", "POSTGRES_PASSWORD")) {
    if (-not (Test-Configured $key)) {
        Stop-Migration "$key is missing from .env - run: uv run pipeline config init"
    }
    Write-Ok "$key configured"
}

# ── 2. Back up what is not derived ────────────────────────────────────

if (-not $BackupTo) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $BackupTo = Join-Path $ProjectRoot "backups\pre-postgres-$stamp"
}

Write-Step "Backing up the manifest, transcripts and minutes"
Write-Note "destination: $BackupTo"

$backupArgs = @("backup", "--to", $BackupTo)
if (-not $IncludeAudio) { $backupArgs += "--no-audio" }
Invoke-Pipeline $backupArgs
if ($LASTEXITCODE -ne 0) {
    Stop-Migration "backup failed - migrating without one is not worth the risk"
}
Write-Ok "backup complete"

$ragStorage = Join-Path $ProjectRoot "rag_storage"
if ($SkipRagStorageCopy) {
    Write-Note "skipping the rag_storage copy (-SkipRagStorageCopy) - no rollback path"
}
elseif (Test-Path -LiteralPath $ragStorage) {
    $ragBackup = Join-Path $BackupTo "rag_storage"
    Write-Note "copying rag_storage aside as the rollback path"
    Copy-Item -LiteralPath $ragStorage -Destination $ragBackup -Recurse -Force
    Write-Ok "old index preserved at $ragBackup"
}
else {
    Write-Note "no rag_storage directory - nothing to preserve"
}

# ── 3. Bring up the Postgres-backed stack ─────────────────────────────

Write-Step "Starting the Postgres-backed stack"

# compose reads the two secrets from .env itself and refuses to start without
# them. The pipeline never learns the Postgres password: only the containers do.
Invoke-Compose @("up", "-d")
if ($LASTEXITCODE -ne 0) { Stop-Migration "docker compose up failed - see the output above" }
Write-Ok "containers started"

Write-Note "waiting for Postgres to accept connections"
if (-not (Wait-ForDoctorCheck -Name "postgres" -TimeoutMinutes 5)) {
    Stop-Migration "Postgres did not come up - see: docker compose logs postgres"
}
Write-Ok "Postgres accepting connections"

Write-Note "waiting for LightRAG (it starts only after Postgres is healthy)"
if (-not (Wait-ForDoctorCheck -Name "lightrag" -TimeoutMinutes 8)) {
    Stop-Migration "LightRAG did not become reachable - see: docker compose logs lightrag"
}
Write-Ok "LightRAG reachable"

# ── 4. Refuse to re-index into the store we are leaving ───────────────

Write-Step "Confirming the index is really on Postgres"

$report = Get-DoctorReport
if (-not $report) { Stop-Migration "could not run pipeline doctor" }

if (-not (Test-DoctorCheck -Report $report -Name "postgres storage")) {
    $storage = Get-DoctorCheck -Report $report -Name "lightrag storage"
    if ($storage) { Write-Bad $storage.detail }
    Stop-Migration ("LightRAG is not reporting Postgres storage. Re-indexing now would " +
        "spend hours rebuilding the same file-based index. Check the LIGHTRAG_*_STORAGE " +
        "settings in docker-compose.yml, then re-run.")
}
Write-Ok "KV, vectors, doc-status and graph all on Postgres"

# ── 5. Re-index the existing minutes ──────────────────────────────────

Write-Step "Re-indexing minutes into the empty Postgres index"
Write-Note "entity extraction is CPU-bound; expect several minutes per meeting"

Invoke-Pipeline @("reindex")
$reindexExit = $LASTEXITCODE
if ($reindexExit -ne 0) {
    Add-Problem 'some meetings did not re-index - see: uv run pipeline status'
}
else {
    Write-Ok "re-index completed"
}

# ── 6. Verify ─────────────────────────────────────────────────────────

Write-Step "Verifying retrieval"

Push-Location $ProjectRoot
try { $answer = & uv run pipeline query $VerifyQuestion --local 2>&1 | Out-String }
finally { Pop-Location }

if ($LASTEXITCODE -ne 0 -or -not $answer.Trim()) {
    Add-Problem "the migrated index returned nothing for a test question"
}
else {
    Write-Ok "the index answered a real question ($($answer.Trim().Length) characters)"
    Write-Host ""
    Write-Host ($answer.Trim() -split "`n" | Select-Object -First 6 | Out-String)
}

Write-Step "Verifying the rest of the pipeline still works"

Invoke-Pipeline @("capture", "--dry-run")
if ($LASTEXITCODE -eq 0) { Write-Ok "Drive capture policy ran cleanly" }
else { Add-Problem "drive capture dry run failed" }

$report = Get-DoctorReport
if ($report) { Write-DoctorReport -Report $report }
else { Add-Problem "could not re-run pipeline doctor" }

# ── Done ──────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "------------------------------------------------------------" -ForegroundColor White

if ($Problems.Count -eq 0) {
    Write-Host "Migration complete and verified." -ForegroundColor Green
    Write-Host ""
    Write-Host "The old file-based index is still on disk at:"
    Write-Host "    $ragStorage"
    Write-Host "and backed up under $BackupTo."
    Write-Host "Delete them once you have run a few real queries against the new index."
    exit 0
}

Write-Host "Migration finished with $($Problems.Count) problem(s):" -ForegroundColor Yellow
foreach ($problem in $Problems) { Write-Host "  - $problem" }
Write-Host ""
Write-Host "Nothing was deleted. To roll back: restore docker-compose.yml, copy"
Write-Host "$BackupTo\rag_storage back over rag_storage\, and run: docker compose up -d"
exit 1
