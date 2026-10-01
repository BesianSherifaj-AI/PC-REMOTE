param([switch]$OpenDashboard)
$ErrorActionPreference = 'Stop'
$runtime = Join-Path $PSScriptRoot '.runtime'
$taskMutex = $null
$taskHasMutex = $false
try {
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    Import-Module (Join-Path $PSScriptRoot 'MAIC-Startup.psm1') -Force
    $taskMutex = [Threading.Mutex]::new($false, (Get-MAICMutexName $PSScriptRoot))
    # An interactive open must wait for the existing startup, not drop its request.
    $mutexWait = if ($OpenDashboard) { 120000 } else { 0 }
    try { $taskHasMutex = $taskMutex.WaitOne($mutexWait) } catch [Threading.AbandonedMutexException] { $taskHasMutex = $true }
    if (!$taskHasMutex) {
        if ($OpenDashboard) { throw 'MAIC is still starting. Check your Wi-Fi, then try Open Dashboard again.' }
        Write-Host 'MAIC is already starting.'; exit 0
    }
    $networkOptions = @{}
    $networkConfig = Join-Path $runtime 'network.json'
    if (Test-Path -LiteralPath $networkConfig) {
        $networkChoice = Get-Content -LiteralPath $networkConfig -Raw | ConvertFrom-Json
        if (!$networkChoice.interfaceAlias -or $networkChoice.interfaceAlias -isnot [string]) { throw 'Invalid saved network selection. Run Install-PCRemote.ps1 -InterfaceAlias with your LAN interface name.' }
        $networkOptions.InterfaceAlias = $networkChoice.interfaceAlias
    }
    $network = Wait-MAICNetwork @networkOptions
    $lanAddress = $network.IPv4Address[0].IPAddress
    $lanPrefix = $network.IPv4Address[0].PrefixLength
    $python = Get-MAICPythonPath -Root $PSScriptRoot
    $serverPath = Join-Path $PSScriptRoot 'maic_server.py'
    $pidPath = Join-Path $runtime 'server.pid'
    $recordPath = Join-Path $runtime 'server-process.json'
    $url = "http://${lanAddress}:8840"
    if (Test-Path -LiteralPath $pidPath) {
        $savedProcessId = (Get-Content -LiteralPath $pidPath -Raw).Trim()
        if ($savedProcessId -match '^\d+$') {
            $running = Get-CimInstance Win32_Process -Filter "ProcessId=$savedProcessId"
            $record = if (Test-Path -LiteralPath $recordPath) { Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json } else { $null }
            if (Test-MAICProcess $running $python $serverPath $record) {
                try { $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 3 } catch { throw 'Existing MAIC server is not responding at the current LAN address. Use Stop, then Start.' }
                if (!$health.ok) { throw 'Existing MAIC server health check failed.' }
                if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'tools\tightvnc\application\tvnserver.exe')) {
                    try { & (Join-Path $PSScriptRoot 'Setup-Desktop-Viewer.ps1') -StartOnly -PythonPath $python | Out-Null }
                    catch { ('Desktop viewer: ' + $_.Exception.Message) | Set-Content -LiteralPath (Join-Path $runtime 'desktop-start-error.log') }
                }
                $url | Set-Content -LiteralPath (Join-Path $runtime 'server.url') -Encoding ASCII
                Write-Host "Already running: $url"
                if ($OpenDashboard) { Start-Process $url }
                exit 0
            }
        }
    }
    if (@(Get-NetTCPConnection -LocalPort 8840 -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
        throw 'Port 8840 is already in use. No existing process was changed.'
    }
    # Workspace-owned VNC only. Its failure must not stop the ordinary dashboard.
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'tools\tightvnc\application\tvnserver.exe')) {
        try { & (Join-Path $PSScriptRoot 'Setup-Desktop-Viewer.ps1') -StartOnly -PythonPath $python | Out-Null }
        catch { ('Desktop viewer: ' + $_.Exception.Message) | Set-Content -LiteralPath (Join-Path $runtime 'desktop-start-error.log') }
    }
    $arguments = '-X utf8 "{0}" --host {1} --port 8840 --network {1}/{2}' -f $serverPath, $lanAddress, $lanPrefix
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime 'server.out.log') -RedirectStandardError (Join-Path $runtime 'server.err.log') -PassThru
    $process.Id | Set-Content -LiteralPath $pidPath -Encoding ASCII
    @{pid=$process.Id; executable=$python; server=$serverPath; startedAt=$process.StartTime.ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        if ($process.HasExited) { throw 'MAIC server exited. Read .runtime/server.err.log on the PC.' }
        $health = $null
        try { $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 1 }
        catch { if ($attempt -eq 39) { throw 'MAIC started but its health check did not respond.' } }
        if ($health.ok) {
            $url | Set-Content -LiteralPath (Join-Path $runtime 'server.url') -Encoding ASCII
            Write-Host "Open on the MAIC: $url"
            if ($OpenDashboard) { Start-Process $url }
            exit 0
        }
    }
    throw 'MAIC started but its health check did not report ready.'
} catch {
    $_.Exception.Message | Set-Content -LiteralPath (Join-Path $runtime 'startup-error.log')
    throw
} finally { if ($taskHasMutex) { $taskMutex.ReleaseMutex() }; if ($taskMutex) { $taskMutex.Dispose() } }
