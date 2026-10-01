param(
    [string]$PythonPath,
    [string]$InterfaceAlias,
    [switch]$DashboardOnly,
    [switch]$WithSpeech,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskRuntime = Join-Path $taskRoot '.runtime'

function Find-PCRemotePython {
    param([string]$ExplicitPath)
    $candidates = @()
    if ($ExplicitPath) {
        $candidates += $ExplicitPath
    } else {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) {
            foreach ($version in @('-3.11', '-3.12')) {
                $found = $null
                try { $found = & $launcher.Source $version -c 'import sys; print(sys.executable)' 2>$null } catch { continue }
                if ($LASTEXITCODE -eq 0 -and $found) { $candidates += ([string]$found).Trim() }
            }
        }
        $command = Get-Command python -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
    }
    foreach ($candidate in $candidates) {
        if (!$candidate -or $candidate -match '[\\/]WindowsApps[\\/]' -or !(Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $verified = $null
        try { $verified = & $candidate -c 'import sys; print(sys.executable) if sys.version_info[:2] in ((3,11),(3,12)) and sys.maxsize > 2**32 else sys.exit(1)' 2>$null } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $verified) { return ([string]$verified).Trim() }
    }
    throw 'Install 64-bit Python 3.11 or 3.12 from python.org, then rerun. You can select it with -PythonPath C:\Python311\python.exe.'
}

if ($env:OS -ne 'Windows_NT' -or ![Environment]::Is64BitOperatingSystem) { throw 'PC Remote requires 64-bit Windows 10 or 11.' }
$taskPython = Find-PCRemotePython $PythonPath
Import-Module (Join-Path $taskRoot 'MAIC-Startup.psm1') -Force
$taskNetworks = @(Get-NetIPConfiguration | Where-Object {
    $_.IPv4DefaultGateway -and $_.IPv4Address -and $_.InterfaceAlias -notmatch 'Tailscale|VPN|Loopback' -and
    $_.IPv4Address[0].IPAddress -notmatch '^(127\.|169\.254\.)'
})
if (!$taskNetworks.Count) { throw 'Connect this PC to its trusted Wi-Fi or Ethernet network, then rerun setup.' }
if (!$InterfaceAlias -and (Test-Path -LiteralPath (Join-Path $taskRuntime 'network.json'))) {
    $taskSavedNetwork = Get-Content -LiteralPath (Join-Path $taskRuntime 'network.json') -Raw | ConvertFrom-Json
    $InterfaceAlias = [string]$taskSavedNetwork.interfaceAlias
}
if (!$InterfaceAlias -and $taskNetworks.Count -gt 1) {
    Write-Host 'Choose the trusted LAN interface for this dashboard:'
    $taskNetworks | Select-Object InterfaceAlias, @{Name='IPv4';Expression={$_.IPv4Address[0].IPAddress}} | Format-Table | Out-Host
    if ($CheckOnly) { throw 'Multiple LAN interfaces found. Rerun with -InterfaceAlias and the exact interface name.' }
    $InterfaceAlias = Read-Host 'Interface name'
}
if (!$InterfaceAlias) { $InterfaceAlias = [string]$taskNetworks[0].InterfaceAlias }
$taskSelected = Wait-MAICNetwork -InterfaceAlias $InterfaceAlias -TimeoutSeconds 0
if (!$DashboardOnly) {
    $taskSevenZip = Join-Path $env:ProgramFiles '7-Zip\7z.exe'
    if (!(Test-Path -LiteralPath $taskSevenZip -PathType Leaf) -and !(Get-Command 7z -ErrorAction SilentlyContinue)) {
        throw 'Desktop viewing needs 7-Zip from 7-zip.org. Install it first, or use -DashboardOnly for app/audio/chat controls.'
    }
}
Write-Host ('Python: ' + $taskPython)
Write-Host ('LAN interface: ' + $taskSelected.InterfaceAlias)
Write-Host 'Base setup: isolated .venv, dependencies, Desktop shortcuts, current-user login startup.'
if (!$DashboardOnly) { Write-Host 'Desktop setup: download and verify official TightVNC and pinned noVNC; user-session mode only.' }
if ($WithSpeech) { Write-Host 'Optional speech: download about 678 MB of Kokoro/Whisper model assets, plus speech runtime dependencies.' }
Write-Host 'Internet sharing, firewall rules, router settings and LM Studio models are not changed.'
if ($CheckOnly) { Write-Host 'Preflight passed. No installation or configuration changes were made.'; return }

# Changing the saved Python path under a running server would break its ownership
# record. Make the operator stop that instance first; never terminate it here.
$taskServerRecord = Join-Path $taskRuntime 'server-process.json'
if (Test-Path -LiteralPath $taskServerRecord) {
    $taskRecord = Get-Content -LiteralPath $taskServerRecord -Raw | ConvertFrom-Json
    $taskRunning = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$taskRecord.pid) -ErrorAction SilentlyContinue
    if ($taskRunning -and (Test-MAICProcess $taskRunning ([string]$taskRecord.executable) ([string]$taskRecord.server) $taskRecord)) {
        throw 'PC Remote is running from this folder. Use its Stop shortcut, then rerun setup.'
    }
}
$taskVenv = Join-Path $taskRoot '.venv'
$taskVenvPython = Join-Path $taskVenv 'Scripts\python.exe'
if (!(Test-Path -LiteralPath $taskVenvPython -PathType Leaf)) {
    & $taskPython -m venv $taskVenv
    if ($LASTEXITCODE -ne 0) { throw 'Creating the project Python environment failed.' }
}
& $taskVenvPython -m pip install --disable-pip-version-check -r (Join-Path $taskRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check internet access and rerun setup.' }
& $taskVenvPython -c 'import sys; sys.path.insert(0,sys.argv[1]); import aiohttp, psutil, PIL, cryptography; import pc_controls, pc_apps, ai_services, desktop_bridge' $taskRoot
if ($LASTEXITCODE -ne 0) { throw 'Installed dependencies could not load. Setup stopped before installing shortcuts.' }
if ($WithSpeech) {
    & $taskVenvPython -X utf8 (Join-Path $taskRoot 'tools\setup_pc_speech.py')
    if ($LASTEXITCODE -ne 0) { throw 'Optional speech setup failed. Rerun without -WithSpeech to install the dashboard only.' }
    & $taskVenvPython -X utf8 (Join-Path $taskRoot 'tools\setup_dictation.py')
    if ($LASTEXITCODE -ne 0) { throw 'Optional dictation setup failed. Rerun setup to retry, or use typed chat.' }
}
if (!$DashboardOnly) {
    & (Join-Path $taskRoot 'Setup-Desktop-Viewer.ps1') -PythonPath $taskVenvPython
}
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $taskRuntime 'network.json'), (@{interfaceAlias=$InterfaceAlias} | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
& (Join-Path $taskRoot 'Install-MAIC-Desktop.ps1') -PythonPath $taskVenvPython
Write-Host ''
Write-Host 'Installed. Use Desktop -> MAIC PC -> Start to open PC Remote.'
Write-Host 'Approve your phone/tablet with Approve-MAIC.ps1 -Address followed by its Wi-Fi IPv4 address.'
Write-Host 'Optional secure internet access is a separate step documented in README.md.'
