#Requires -Version 5.1
<#
.SYNOPSIS
    One-command setup for the Meeting Minutes Compiler on Windows.

.DESCRIPTION
    Safe to re-run: never overwrites an existing .env, skips anything already done.

    Afterwards you still need to do two things by hand, because nobody can do them
    for you:
      1. Put a HuggingFace token in .env
      2. Accept two model licences on huggingface.co
    The script tells you exactly where at the end.

.EXAMPLE
    .\setup.ps1
    .\setup.ps1 -SkipModels   # skip multi-GB model pull (if resuming)
#>
[CmdletBinding()]
param(
    [switch]$SkipModels
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# ── Helpers ───────────────────────────────────────────────────────────

function Write-Step  { param([string]$msg) Write-Host "`n==> $msg" -ForegroundColor White }
function Write-Ok    { param([string]$msg) Write-Host "    [ok]   $msg" -ForegroundColor Green }
function Write-Warn  { param([string]$msg) Write-Host "    [warn] $msg" -ForegroundColor Yellow }
function Write-Fail  { param([string]$msg) Write-Host "    [FAIL] $msg" -ForegroundColor Red }
function Test-Command { param([string]$cmd) [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# Work from the project root regardless of where the script is called from.
$projectRoot = Split-Path -Parent $PSScriptRoot
if ($PSScriptRoot -eq (Split-Path -Parent $PSCommandPath)) {
    # Script is at project root (not in scripts/)
    $projectRoot = $PSScriptRoot
}
# If setup.ps1 is at the project root itself:
if (Test-Path (Join-Path $PSScriptRoot "pyproject.toml")) {
    $projectRoot = $PSScriptRoot
}
Push-Location $projectRoot

$missing = $false

try {

# ── 1. Prerequisites ──────────────────────────────────────────────────

Write-Step "Checking prerequisites"

# ffmpeg
if (Test-Command "ffmpeg") {
    $ffVer = (ffmpeg -version 2>&1 | Select-Object -First 1) -replace 'ffmpeg version\s+', ''
    Write-Ok "ffmpeg $ffVer"
} else {
    Write-Warn "ffmpeg not found - installing via winget"
    try {
        winget install --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements --silent 2>&1 | Out-Null
        # winget installs to a standard location; add it to current session PATH
        $ffmpegPaths = @(
            "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin"
            "C:\Program Files\FFmpeg\bin"
            "C:\ffmpeg\bin"
        )
        foreach ($pattern in $ffmpegPaths) {
            $resolved = Resolve-Path $pattern -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($resolved -and (Test-Path (Join-Path $resolved.Path "ffmpeg.exe"))) {
                $env:PATH = "$($resolved.Path);$env:PATH"
                break
            }
        }
        if (Test-Command "ffmpeg") {
            Write-Ok "ffmpeg installed and available in this session"
        } else {
            Write-Warn "ffmpeg installed but not on PATH yet - restart your terminal after setup"
        }
    } catch {
        Write-Fail "ffmpeg install failed: $_"
        Write-Warn "install manually: winget install Gyan.FFmpeg"
        $missing = $true
    }
}

# Docker
if (Test-Command "docker") {
    $dockerOk = $false
    try {
        docker info 2>&1 | Out-Null
        $dockerOk = $true
    } catch {}
    if ($dockerOk) {
        Write-Ok "docker running"
    } else {
        Write-Fail "docker installed but not running (start Docker Desktop)"
        $missing = $true
    }
} else {
    Write-Fail "docker not found - needed for the search index"
    Write-Warn "install: https://docs.docker.com/desktop/install/windows-install/"
    $missing = $true
}

# uv
if (Test-Command "uv") {
    $uvVer = (uv --version 2>&1) -replace 'uv\s+', ''
    Write-Ok "uv $uvVer"
} else {
    Write-Warn "uv not found - installing"
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
        if (Test-Command "uv") {
            Write-Ok "uv installed"
        } else {
            Write-Fail "uv install failed"
            $missing = $true
        }
    } catch {
        Write-Fail "uv install failed: $_"
        $missing = $true
    }
}

# LLM providers
$providers = @("gemini", "codex", "claude") | Where-Object { Test-Command $_ }
if ($providers.Count -gt 0) {
    Write-Ok "LLM providers found: $($providers -join ', ')"
} else {
    Write-Warn "none of gemini/codex/claude on PATH"
    Write-Warn "minutes cannot be written until at least one is installed and logged in"
}

if ($missing) {
    Write-Host "`nInstall the missing prerequisites above, then re-run .\setup.ps1" -ForegroundColor Red
    return
}

# ── 2. Secrets ────────────────────────────────────────────────────────

Write-Step "Configuring .env"

if (Test-Path ".env") {
    Write-Ok ".env already exists - leaving it alone"
} else {
    Copy-Item ".env.example" ".env"

    # Generate the two secrets that can be auto-generated. HF_TOKEN cannot be.
    $envContent = Get-Content ".env" -Raw
    $genSecret = { python -c "import secrets; print(secrets.token_urlsafe(32))" }

    $apiKey = & $genSecret
    $pgPass = & $genSecret

    $envContent = $envContent -replace "(?m)^MMC_LIGHTRAG_API_KEY=\s*$", "MMC_LIGHTRAG_API_KEY=$apiKey"
    $envContent = $envContent -replace "(?m)^POSTGRES_PASSWORD=\s*$", "POSTGRES_PASSWORD=$pgPass"

    Set-Content ".env" -Value $envContent -NoNewline
    Write-Ok "created .env with generated secrets (MMC_LIGHTRAG_API_KEY, POSTGRES_PASSWORD)"
}

# Owner name
$envText = Get-Content ".env" -Raw
if ($envText -notmatch "(?m)^MMC_OWNER_NAME=") {
    $owner = Read-Host "    Your name (used for speaker identification in 1:1 recordings, Enter to skip)"
    if ($owner) {
        Add-Content ".env" "`nMMC_OWNER_NAME=$owner"
        Write-Ok "set MMC_OWNER_NAME=$owner"
    } else {
        Write-Warn "skipped - set MMC_OWNER_NAME in .env later"
    }
}

# ── 3. Python dependencies ────────────────────────────────────────────

Write-Step "Installing Python dependencies"
Write-Warn "the ASR extra pulls torch - this can take several minutes"
uv sync --extra asr --extra dev
Write-Ok "dependencies installed"

# ── 4. Services ───────────────────────────────────────────────────────

Write-Step "Starting services (Postgres, LightRAG, Ollama)"
docker compose up -d
Write-Ok "containers started"

# Wait for Ollama
Write-Host "    waiting for Ollama" -NoNewline
for ($i = 0; $i -lt 30; $i++) {
    try {
        docker compose exec -T ollama ollama list 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host ""
            Write-Ok "Ollama ready"
            break
        }
    } catch {}
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 2
}

# Wait for Postgres
Write-Host "    waiting for Postgres" -NoNewline
for ($i = 0; $i -lt 30; $i++) {
    try {
        docker exec mmc-postgres pg_isready -U lightrag -d rag 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host ""
            Write-Ok "Postgres ready"
            break
        }
    } catch {}
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 2
}

# Pull models
if (-not $SkipModels) {
    Write-Step "Pulling local models (a few GB, one time)"
    foreach ($model in @("qwen3:4b", "mxbai-embed-large")) {
        $listed = docker compose exec -T ollama ollama list 2>&1
        $shortName = ($model -split ":")[0]
        if ($listed -match [regex]::Escape($shortName)) {
            Write-Ok "$model already present"
        } else {
            Write-Host "    pulling $model..."
            docker compose exec -T ollama ollama pull $model
            if ($LASTEXITCODE -eq 0) { Write-Ok "$model pulled" }
            else { Write-Warn "$model pull failed - retry: docker compose exec ollama ollama pull $model" }
        }
    }
}

# ── 5. Initialise ─────────────────────────────────────────────────────

Write-Step "Creating folders and database"
uv run pipeline init
Write-Ok "ready"

# ── 6. Verify ─────────────────────────────────────────────────────────

Write-Step "Running preflight checks"
uv run pipeline doctor
$doctorExit = $LASTEXITCODE

# ── Done ──────────────────────────────────────────────────────────────

Write-Host "`n────────────────────────────────────────────────────────" -ForegroundColor White

if ($doctorExit -eq 0) {
    Write-Host "Setup complete.`n" -ForegroundColor Green
} else {
    Write-Host "Setup done, but preflight found problems.`n" -ForegroundColor Yellow
    Write-Host "Almost always this is the HuggingFace token. Two steps:`n"
    Write-Host "  1. Create a " -NoNewline
    Write-Host "read" -ForegroundColor White -NoNewline
    Write-Host " token:"
    Write-Host "     https://huggingface.co/settings/tokens" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  2. Accept BOTH licences (the token alone is not enough):"
    Write-Host "     https://hf.co/pyannote/speaker-diarization-3.1" -ForegroundColor DarkGray
    Write-Host "     https://hf.co/pyannote/segmentation-3.0" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  3. Paste the token into .env as HF_TOKEN=hf_..."
    Write-Host ""
    Write-Host "Then re-run: " -NoNewline
    Write-Host "uv run pipeline doctor" -ForegroundColor White
    Write-Host ""
}

Write-Host "Your first meeting:`n" -ForegroundColor White
Write-Host "  Copy-Item ~\some-recording.m4a inbox\"
Write-Host "  uv run pipeline run`n"
Write-Host "Expect ~30-50 min for a one-hour recording on CPU. Then:`n"
Write-Host "  Get-Content minutes\*.md                         # read what it wrote"
Write-Host "  uv run pipeline query `"what did we decide?`"`n"
Write-Host "Full guide: docs\USER_GUIDE.md`n" -ForegroundColor White

} finally {
    Pop-Location
}
