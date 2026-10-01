param([Parameter(Mandatory = $true)][string]$Address)
$ErrorActionPreference = 'Stop'
$taskClientAddress = $null
if (-not [Net.IPAddress]::TryParse($Address, [ref]$taskClientAddress) -or $taskClientAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'Use the MAIC IPv4 address shown in its Wi-Fi settings.' }
$runtimePath = Join-Path $PSScriptRoot '.runtime'
$configPath = Join-Path $runtimePath 'control-access.json'
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$clients = @()
if (Test-Path -LiteralPath $configPath) {
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $clients = @($config.clients)
}
$clients = @($clients + $taskClientAddress.ToString() | Select-Object -Unique)
@{clients = @($clients)} | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding ASCII
Write-Host "Approved $Address for dashboard audio controls, saved websites, and the listed PC apps."
