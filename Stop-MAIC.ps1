$ErrorActionPreference = 'Stop'
$runtime = Join-Path $PSScriptRoot '.runtime'
$taskMutex = $null
$taskHasMutex = $false
try {
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    Import-Module (Join-Path $PSScriptRoot 'MAIC-Startup.psm1') -Force
    $taskMutex = [Threading.Mutex]::new($false, (Get-MAICMutexName $PSScriptRoot))
    try { $taskHasMutex = $taskMutex.WaitOne(10000) } catch [Threading.AbandonedMutexException] { $taskHasMutex = $true }
    if (!$taskHasMutex) { throw 'MAIC is starting. Wait a moment, then use Stop.' }
    $pidPath = Join-Path $runtime 'server.pid'
    $recordPath = Join-Path $runtime 'server-process.json'
    if (Test-Path -LiteralPath $pidPath) {
        $savedProcessId = (Get-Content -LiteralPath $pidPath -Raw).Trim()
        if ($savedProcessId -notmatch '^\d+$') { throw 'Invalid MAIC process record.' }
        $running = Get-CimInstance Win32_Process -Filter "ProcessId=$savedProcessId"
        $record = if (Test-Path -LiteralPath $recordPath) { Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json } else { $null }
        if ($running) {
            if (!(Test-MAICProcess $running (Get-MAICPythonPath -Root $PSScriptRoot) (Join-Path $PSScriptRoot 'maic_server.py') $record)) {
                throw 'Saved PID belongs to another process; it was left running.'
            }
            $owned = Get-Process -Id ([int]$savedProcessId)
            Stop-Process -Id ([int]$savedProcessId)
            if (!$owned.WaitForExit(5000)) { throw 'MAIC process did not stop.' }
        }
        Remove-Item -LiteralPath $pidPath
        if (Test-Path -LiteralPath $recordPath) { Remove-Item -LiteralPath $recordPath }
    }
    & (Join-Path $PSScriptRoot 'Setup-Desktop-Viewer.ps1') -Stop | Out-Null
    Write-Host 'MAIC dashboard and its desktop viewer stopped.'
} catch { $_.Exception.Message | Set-Content -LiteralPath (Join-Path $runtime 'stop-error.log'); throw }
finally { if ($taskHasMutex) { $taskMutex.ReleaseMutex() }; if ($taskMutex) { $taskMutex.Dispose() } }
