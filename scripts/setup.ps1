#Requires -Version 5.1
<#
.SYNOPSIS
    One-command Windows setup for the meeting minutes compiler.

.DESCRIPTION
    Installs what can be installed, generates the secrets that can be generated,
    starts the services, and then verifies the result. Safe to re-run: it never
    overwrites an existing .env and skips anything already done.

        .\scripts\setup.ps1

    Two things it cannot do for you, because they are tied to your accounts and
    require a browser:

      1. A HuggingFace read token, plus manual acceptance of two model licences.
         Without it, transcripts have no speaker labels, which means action items
         with no owners.
      2. Google Drive consent. Code can open the consent screen; it cannot
         approve on your behalf.

    The script prompts for the first, opens the second, and verifies both.

    No secret is ever printed. Values are read from the console with the input
    hidden and handed to `pipeline config set` over stdin - never as an argument,
    which would put them in shell history and in the process table. Everything
    afterwards reports `configured` or `missing`.

.PARAMETER VerifyOnly
    Skip installation and run only the verification phase.

.PARAMETER SkipScheduledTasks
    Do not register the nightly batch or the dashboard at sign-in.

.PARAMETER SkipAsr
    Install without the ASR extra. Much faster, but transcription will not run -
    for a machine that only queries the corpus.

.PARAMETER Owner
    Your own name, used to tell who is who in two-person recordings. Prompted for
    if not supplied and not already in .env.
#>
[CmdletBinding()]
param(
    [switch]$VerifyOnly,
    [switch]$SkipScheduledTasks,
    [switch]$SkipAsr,
    [string]$Owner
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "lib.ps1")

function Update-SessionPath {
    <#
        winget and choco write to the machine and user PATH, which the already
        running process does not see. Without this, ffmpeg is installed and still
        "not found" until the window is closed and reopened - and the person
        running setup reasonably concludes the install failed.
    #>
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ";"
}

# ── 1. Prerequisites ──────────────────────────────────────────────────

function Install-Prerequisite {
    <#
        Installs one tool through whichever Windows package manager is present.
        Returns $true if the tool is on PATH afterwards.
    #>
    param(
        [Parameter(Mandatory)][string]$Command,
        [string]$WingetId,
        [string]$ChocoId,
        [string]$Consequence
    )

    if (Test-Command $Command) {
        Write-Ok "$Command found"
        return $true
    }

    if ($WingetId -and (Test-Command "winget")) {
        Write-Note "installing $Command via winget ($WingetId)"
        & winget install --id $WingetId --exact --silent `
            --accept-package-agreements --accept-source-agreements | Out-Null
    }
    elseif ($ChocoId -and (Test-Command "choco")) {
        Write-Note "installing $Command via Chocolatey ($ChocoId)"
        & choco install $ChocoId -y --no-progress | Out-Null
    }
    else {
        Add-Problem "$Command is missing and no package manager is available - $Consequence"
        Write-Note "install App Installer (winget) from the Microsoft Store, then re-run"
        return $false
    }

    Update-SessionPath
    if (Test-Command $Command) {
        Write-Ok "$Command installed"
        return $true
    }

    Add-Problem "$Command still not on PATH after install - $Consequence"
    Write-Note "close this window, open a new one, and re-run .\scripts\setup.ps1"
    return $false
}

function Start-DockerEngine {
    <#
        Docker Desktop being installed is not the same as Docker Desktop running,
        and the difference looks identical from the command line: `docker` exists
        and every call fails.
    #>
    if (-not (Test-Command "docker")) {
        Add-Problem "docker not found - LightRAG, Postgres and Ollama cannot run"
        Write-Note "install Docker Desktop: https://docs.docker.com/desktop/install/windows-install/"
        return $false
    }

    & docker info 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "docker running"
        return $true
    }

    $desktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path -LiteralPath $desktop) {
        Write-Note "Docker Desktop is not running - starting it (this takes a minute)"
        Start-Process -FilePath $desktop | Out-Null
        $deadline = (Get-Date).AddMinutes(3)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 5
            & docker info 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "docker running"
                return $true
            }
        }
    }

    Add-Problem "docker is installed but not responding"
    Write-Note "start Docker Desktop, wait for the whale icon to settle, then re-run"
    return $false
}

function Test-Prerequisites {
    Write-Step "Checking prerequisites"

    # Gyan.FFmpeg is the build winget ships; the choco package is the fallback for
    # machines managed with Chocolatey.
    Install-Prerequisite -Command "ffmpeg" -WingetId "Gyan.FFmpeg" -ChocoId "ffmpeg" `
        -Consequence "transcription cannot run at all" | Out-Null

    Install-Prerequisite -Command "uv" -WingetId "astral-sh.uv" -ChocoId "uv" `
        -Consequence "the Python environment cannot be built" | Out-Null

    Start-DockerEngine | Out-Null

    $providers = @("gemini", "codex", "claude") | Where-Object { Test-Command $_ }
    if ($providers) {
        Write-Ok ("LLM providers found: " + ($providers -join ", "))
    }
    else {
        Write-Note "none of gemini/codex/claude on PATH - minutes cannot be written yet"
        Write-Note "install one and sign in; the chain falls through to whichever answers"
    }
}

# ── 2. Configuration ──────────────────────────────────────────────────

function Set-SecretFromConsole {
    <#
        Prompts for a secret with the input hidden and hands it to the pipeline
        over stdin. The value never becomes a command-line argument, never lands
        in PSReadLine history, and is never echoed.
    #>
    param(
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][string]$Prompt
    )

    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        if ([string]::IsNullOrWhiteSpace($plain)) {
            Write-Note "nothing entered - $Key left unset"
            return $false
        }
        Push-Location $ProjectRoot
        try {
            $plain | & uv run pipeline config set $Key | Out-Null
        }
        finally {
            Pop-Location
        }
        if ($LASTEXITCODE -ne 0) {
            Add-Problem "could not write $Key to .env"
            return $false
        }
        Write-Ok "$Key configured"
        return $true
    }
    finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        Remove-Variable -Name plain -ErrorAction SilentlyContinue
    }
}

function Initialize-Configuration {
    Write-Step "Configuring .env"

    Invoke-Pipeline @("config", "init")
    if ($LASTEXITCODE -ne 0) {
        Add-Problem "could not create .env"
        return
    }

    if (-not (Test-Configured "MMC_OWNER_NAME")) {
        $name = $Owner
        if (-not $name) {
            $name = Read-Host -Prompt "    Your name (used to tell who is who in 1:1 recordings)"
        }
        if ($name) {
            Push-Location $ProjectRoot
            try { $name | & uv run pipeline config set MMC_OWNER_NAME | Out-Null }
            finally { Pop-Location }
            Write-Ok "MMC_OWNER_NAME configured"
        }
        else {
            Write-Note "skipped - set MMC_OWNER_NAME in .env later"
        }
    }
    else {
        Write-Ok "MMC_OWNER_NAME configured"
    }

    if (Test-Configured "HF_TOKEN") {
        Write-Ok "HF_TOKEN configured"
        return
    }

    Write-Host ""
    Write-Host "    A HuggingFace read token is needed for speaker diarization." -ForegroundColor White
    Write-Host "    Without it, transcripts have no speaker labels, which means"
    Write-Host "    action items with nobody assigned. Two steps, both in a browser:"
    Write-Host ""
    Write-Host "      1. Create a READ token:  https://huggingface.co/settings/tokens"
    Write-Host "      2. Accept BOTH licences (the token alone is not enough):"
    Write-Host "           https://hf.co/pyannote/speaker-diarization-3.1"
    Write-Host "           https://hf.co/pyannote/segmentation-3.0"
    Write-Host ""
    Write-Host "    Leave the prompt empty to skip; re-run this script when you have one."
    Write-Host ""

    Set-SecretFromConsole -Key "HF_TOKEN" -Prompt "    Paste the token (input hidden)" | Out-Null
}

# ── 3. Dependencies ───────────────────────────────────────────────────

function Install-PythonEnvironment {
    Write-Step "Installing Python dependencies"
    if (-not (Test-Command "uv")) {
        Add-Problem "uv is missing - skipping dependency install"
        return
    }
    if ($SkipAsr) {
        Write-Note "skipping the ASR extra (-SkipAsr) - transcription will not run"
        $syncArgs = @("sync", "--extra", "dev")
    }
    else {
        Write-Note "the ASR extra pulls torch - this can take several minutes"
        $syncArgs = @("sync", "--extra", "asr", "--extra", "dev")
    }

    Push-Location $ProjectRoot
    try { & uv @syncArgs }
    finally { Pop-Location }

    if ($LASTEXITCODE -ne 0) {
        Add-Problem "uv sync failed - nothing below will work until it does"
        return
    }
    Write-Ok "dependencies installed"
}

# ── 4. Services ───────────────────────────────────────────────────────

function Start-Services {
    Write-Step "Starting services (Postgres, LightRAG, Ollama)"
    if (-not (Test-Command "docker")) {
        Add-Problem "docker is missing - the index cannot start"
        return
    }

    # compose reads .env itself for the two secrets, and refuses to start without
    # them. That refusal is the design: this index holds every decision and
    # customer conversation in the corpus.
    Invoke-Compose @("up", "-d")
    if ($LASTEXITCODE -ne 0) {
        Add-Problem "docker compose up failed - see the output above"
        return
    }
    Write-Ok "containers started"

    Write-Note "waiting for Postgres"
    $deadline = (Get-Date).AddMinutes(3)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        & docker exec mmc-postgres pg_isready 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 3
    }
    if ($ready) { Write-Ok "Postgres accepting connections" }
    else { Add-Problem 'Postgres did not become ready - see: docker compose logs postgres' }

    Write-Note "waiting for Ollama"
    $deadline = (Get-Date).AddMinutes(3)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        & docker exec mmc-ollama ollama list 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 3
    }
    if (-not $ready) {
        Add-Problem 'Ollama did not become ready - see: docker compose logs ollama'
        return
    }
    Write-Ok "Ollama ready"

    Write-Step "Pulling local models (a few GB, one time)"
    $listed = & docker exec mmc-ollama ollama list 2>$null | Out-String
    foreach ($model in @("qwen3:4b", "mxbai-embed-large")) {
        $stem = $model.Split(":")[0]
        if ($listed -match [regex]::Escape($stem)) {
            Write-Ok "$model already present"
            continue
        }
        Write-Note "pulling $model"
        & docker exec mmc-ollama ollama pull $model
        if ($LASTEXITCODE -eq 0) { Write-Ok "$model pulled" }
        else { Add-Problem "could not pull $model - indexing will fail without it" }
    }
}

# ── 5. Google Drive ───────────────────────────────────────────────────

function Initialize-Drive {
    Write-Step "Google Drive capture"

    if (-not (Test-Configured "MMC_DRIVE_FUTURE_FOLDER_ID")) {
        Write-Note "not configured - only files dropped into inbox\ will be processed"
        Write-Note "set MMC_DRIVE_FUTURE_FOLDER_ID in .env to the private recordings folder id"
        return
    }

    $appData = Join-Path $env:LOCALAPPDATA "MeetingMinutesCompiler"
    $clientFile = Join-Path $appData "drive-client.json"
    $tokenFile = Join-Path $appData "drive-token.json"

    if (-not (Test-Path -LiteralPath $clientFile)) {
        Write-Note "OAuth desktop client missing: $clientFile"
        Write-Host ""
        Write-Host "    Google requires an interactive consent screen, and the client file" -ForegroundColor White
        Write-Host "    identifies this machine to your own project. One time, in a browser:"
        Write-Host ""
        Write-Host "      1. https://console.cloud.google.com/apis/credentials"
        Write-Host "      2. Create Credentials -> OAuth client ID -> Desktop app"
        Write-Host "      3. Download the JSON and save it as:"
        Write-Host "           $clientFile"
        Write-Host ""
        Write-Host "    Then re-run this script, or just: uv run pipeline auth-drive"
        Write-Host ""
        return
    }

    if (Test-Path -LiteralPath $tokenFile) {
        Write-Ok "Drive already authorized"
        return
    }

    Write-Note "opening the Google consent screen - approve read-only Drive access"
    Invoke-Pipeline @("auth-drive")
    if ($LASTEXITCODE -eq 0) { Write-Ok "Drive authorized" }
    else { Add-Problem 'Drive authorization did not complete - retry: uv run pipeline auth-drive' }
}

# ── 6. Scheduled tasks ────────────────────────────────────────────────

function Install-ScheduledTasks {
    Write-Step "Scheduling unattended operation"
    if ($SkipScheduledTasks) {
        Write-Note "skipped (-SkipScheduledTasks)"
        Write-Note "without a scheduled task nothing runs unattended; the corpus stops growing"
        return
    }

    # No -Owner here on purpose. `pipeline run` defaults it from MMC_OWNER_NAME in
    # .env, so the name lives in one place; baking it into the task argument means
    # correcting a misspelling requires re-registering the task.
    try {
        & (Join-Path $PSScriptRoot "install-nightly-task.ps1")
        Write-Ok "nightly batch scheduled"
    }
    catch {
        Add-Problem "could not register the nightly task: $($_.Exception.Message)"
    }

    try {
        & (Join-Path $PSScriptRoot "install-dashboard-task.ps1")
        Write-Ok "dashboard scheduled at sign-in"
    }
    catch {
        Write-Note "could not register the dashboard task: $($_.Exception.Message)"
    }
}

# ── 7. Verification ───────────────────────────────────────────────────

function Invoke-Verification {
    Write-Step "Verifying the installation"

    $report = Get-DoctorReport
    if (-not $report) {
        Add-Problem 'could not run pipeline doctor - the Python environment is not usable'
        return
    }

    Write-DoctorReport -Report $report

    # Doctor proves Drive is reachable. This proves the capture policy runs
    # end to end - folder listing, date parsing, the backfill cutoff - without
    # downloading anything.
    Write-Step "Drive capture dry run"
    Invoke-Pipeline @("capture", "--dry-run")
    if ($LASTEXITCODE -eq 0) { Write-Ok "capture policy ran cleanly" }
    else { Add-Problem "drive capture dry run failed" }

    Write-Step "Summary"
    $expected = @(
        @{ Name = "postgres"; Label = "Postgres storage" },
        @{ Name = "postgres storage"; Label = "LightRAG on Postgres" },
        @{ Name = "diarization"; Label = "Speaker diarization" },
        @{ Name = "dashboard"; Label = "Dashboard" },
        @{ Name = "nightly task"; Label = "Nightly schedule" }
    )
    foreach ($item in $expected) {
        $check = Get-DoctorCheck -Report $report -Name $item.Name
        if (-not $check) { Write-Note "$($item.Label): not reported"; continue }
        if ($check.status -eq "ok") { Write-Ok "$($item.Label): ok" }
        elseif ($check.status -eq "warn") { Write-Note "$($item.Label): $($check.detail)" }
        else { Write-Bad "$($item.Label): $($check.detail)" }
    }
}

# ── Main ──────────────────────────────────────────────────────────────

if (-not $VerifyOnly) {
    Test-Prerequisites
    Initialize-Configuration
    Install-PythonEnvironment
    Start-Services

    Write-Step "Creating folders and the manifest"
    Invoke-Pipeline @("init")
    if ($LASTEXITCODE -eq 0) { Write-Ok "ready" }
    else { Add-Problem "pipeline init failed" }

    Initialize-Drive
    Install-ScheduledTasks
}

Invoke-Verification

Write-Host ""
Write-Host "------------------------------------------------------------" -ForegroundColor White

if ($Problems.Count -eq 0) {
    Write-Host "Setup complete." -ForegroundColor Green
    Write-Host ""
    Write-Host "Your first meeting:"
    Write-Host "    copy C:\path\to\recording.m4a .\inbox\"
    Write-Host "    uv run pipeline run"
    Write-Host ""
    Write-Host "Expect ~30-50 min for a one-hour recording on CPU. Then:"
    Write-Host "    uv run pipeline dashboard --open"
    Write-Host "    uv run pipeline query `"what did we decide?`""
    exit 0
}

Write-Host "Setup finished with $($Problems.Count) problem(s):" -ForegroundColor Yellow
foreach ($problem in $Problems) { Write-Host "  - $problem" }
Write-Host ""
Write-Host "Fix those, then re-run:  .\scripts\setup.ps1 -VerifyOnly"
exit 1
