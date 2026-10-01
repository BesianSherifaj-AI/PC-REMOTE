$ErrorActionPreference = 'Stop'
$urlPath = Join-Path $PSScriptRoot '.runtime\server.url'
if (Test-Path -LiteralPath $urlPath) {
    $url = (Get-Content -LiteralPath $urlPath -Raw).Trim()
    if ($url -match '^http://(?:\d{1,3}\.){3}\d{1,3}:8840$') {
        $health = $null
        try { $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 2 } catch {}
        if ($health.ok) { Start-Process $url; exit 0 }
    }
}
& (Join-Path $PSScriptRoot 'Start-MAIC.ps1') -OpenDashboard
