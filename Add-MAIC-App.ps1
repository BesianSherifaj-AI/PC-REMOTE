param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$Port,
    [string]$Path = '/',
    [string]$Description = ''
)
$ErrorActionPreference = 'Stop'
if ($Name.Length -gt 80 -or [string]::IsNullOrWhiteSpace($Name)) { throw 'Choose a short app name.' }
if ($Description.Length -gt 240) { throw 'Description is too long.' }
if (-not $Path.StartsWith('/') -or $Path.StartsWith('//') -or $Path.Length -gt 2048 -or $Path.Contains('\') -or $Path -match '[\x00-\x1f]') { throw 'Path must be a local web path such as /.' }
$configPath = Join-Path $PSScriptRoot 'apps.json'
$apps = @()
if (Test-Path -LiteralPath $configPath) {
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $apps = @($config.apps)
}
if (@($apps | Where-Object { $_.name -eq $Name }).Count) { throw 'An app with that name is already configured.' }
$entry = [PSCustomObject]@{id = [guid]::NewGuid().ToString('N'); name = $Name; description = $Description; port = $Port; path = $Path}
$apps += $entry
@{apps = @($apps)} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $configPath -Encoding UTF8
Write-Host "Added '$Name'. Keep its web server running and reachable from your Wi-Fi. Refresh the MAIC dashboard."
