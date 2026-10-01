param([Parameter(Mandatory=$true)][ValidateSet('Start', 'Stop', 'Open', 'Help')][string]$Action)
$ErrorActionPreference = 'Stop'
$runtime = Join-Path $PSScriptRoot '.runtime'
try {
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    switch ($Action) {
        'Start' { & (Join-Path $PSScriptRoot 'Start-MAIC.ps1') -OpenDashboard }
        'Open' { & (Join-Path $PSScriptRoot 'Open-MAIC.ps1') }
        'Help' { Start-Process -FilePath (Join-Path $PSScriptRoot 'Help.html') }
        'Stop' {
            & (Join-Path $PSScriptRoot 'Stop-MAIC.ps1')
            $shell = New-Object -ComObject WScript.Shell
            try { [void]$shell.Popup('MAIC PC has stopped. Double-click Start to run it again.', 4, 'MAIC PC', 64) }
            finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
        }
    }
    exit 0
} catch {
    $detail = $_.Exception.Message
    $logPath = Join-Path $runtime 'shortcut-error.log'
    try { ('{0}: {1}' -f $Action, $detail) | Set-Content -LiteralPath $logPath -Encoding UTF8 } catch {}
    $shell = New-Object -ComObject WScript.Shell
    try { [void]$shell.Popup("MAIC PC could not complete $Action.`r`n`r`n$detail`r`n`r`nDetails are saved in:`r`n$logPath", 30, 'MAIC PC - action failed', 16) }
    finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
    exit 1
}
