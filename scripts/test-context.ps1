[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Query,
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [ValidateRange(1, 50)]
    [int]$Limit = 8
)

$ErrorActionPreference = "Stop"
$address = "http://127.0.0.1:$Port/api/context/search"
$body = @{
    query = $Query
    limit = $Limit
    max_chars = 5000
} | ConvertTo-Json

$result = Invoke-RestMethod `
    -Uri $address `
    -Method Post `
    -ContentType "application/json" `
    -Body $body `
    -TimeoutSec 12

$kinds = @($result.items | Group-Object -Property kind | ForEach-Object {
    "$($_.Name)=$($_.Count)"
}) -join ", "
$fresh = if ($result.index.fresh_through) { $result.index.fresh_through } else { "unknown" }

# Deliberately print metadata only. Context text is private meeting content.
Write-Host "Context smoke passed: $(@($result.items).Count) item(s) [$kinds]"
Write-Host "Index: $($result.index.status) via $($result.index.backend), fresh through $fresh"
