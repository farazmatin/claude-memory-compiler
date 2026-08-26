[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$address = "http://127.0.0.1:$Port"
$logDirectory = Join-Path $projectRoot "logs"
$standardOutput = Join-Path $logDirectory "meeting-memory-dashboard.out.log"
$standardError = Join-Path $logDirectory "meeting-memory-dashboard.err.log"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment not found. Run 'uv sync' in $projectRoot first."
}

function Test-DashboardAvailable {
    try {
        $response = Invoke-WebRequest -Uri "$address/style.css" -UseBasicParsing -TimeoutSec 1
        return $response.StatusCode -eq 200 -or $response.StatusCode -eq 401
    }
    catch {
        if ($_.Exception.Response -and ($_.Exception.Response.StatusCode.value__ -in 200, 401)) {
            return $true
        }
        return $false
    }
}

if (-not (Test-DashboardAvailable)) {
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $process = Start-Process `
        -FilePath $python `
        -ArgumentList "-m", "pipeline.cli", "dashboard", "--port", $Port `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $standardOutput `
        -RedirectStandardError $standardError `
        -PassThru

    # Importing the dashboard can take more than ten seconds on Windows after a
    # cold start (notably while Python warms its dependency cache).  The process
    # may already be listening by then, so a short deadline reports a false
    # failure and leaves the user thinking the sign-in launcher is broken.
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (Test-DashboardAvailable) {
            break
        }
        if ($process.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 250
    }

    if (-not (Test-DashboardAvailable)) {
        $exitDetail = if ($process.HasExited) { " (process exit code $($process.ExitCode))" } else { "" }
        throw "Meeting Memory did not start at $address$exitDetail. Check $standardError."
    }
}

Start-Process $address
Write-Host "Meeting Memory is open at $address"
