function Get-MAICPythonPath {
    param([string]$Root, [string]$PythonPath)
    $configPath = Join-Path $Root '.runtime\launcher.json'
    if (!$PythonPath -and (Test-Path -LiteralPath $configPath)) {
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        $pythonPath = [string]$config.pythonPath
    } elseif (!$PythonPath) {
        $pythonPath = (Get-Command python -ErrorAction Stop).Source
    }
    if (!$pythonPath -or $pythonPath -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+[\\/])' -or
        $pythonPath -match '[\\/]WindowsApps[\\/]' -or
        [IO.Path]::GetFileName($pythonPath) -ne 'python.exe' -or
        !(Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw 'The configured Python installation is unavailable. Run Install-MAIC-Desktop.ps1 from the project to repair it.'
    }
    return [IO.Path]::GetFullPath($pythonPath)
}
function Wait-MAICNetwork {
    param([int]$TimeoutSeconds = 90, [string]$InterfaceAlias, [scriptblock]$ReadNetwork = { Get-NetIPConfiguration },
          [scriptblock]$Pause = { param($seconds) Start-Sleep -Seconds $seconds })
    for ($elapsed = 0; $elapsed -le $TimeoutSeconds; $elapsed += 2) {
        $connections = @(& $ReadNetwork | Where-Object {
            $_.IPv4DefaultGateway -and $_.IPv4Address -and $_.InterfaceAlias -notmatch 'Tailscale|VPN|Loopback' -and
            $_.IPv4Address[0].IPAddress -notmatch '^(127\.|169\.254\.)' -and
            (!$InterfaceAlias -or $_.InterfaceAlias -eq $InterfaceAlias)
        })
        if ($connections.Count -eq 1) { return $connections[0] }
        if ($connections.Count -gt 1) { throw 'More than one LAN connection has a gateway. Run Install-PCRemote.ps1 -InterfaceAlias with the intended interface.' }
        if ($elapsed -lt $TimeoutSeconds) { & $Pause 2 }
    }
    throw 'No LAN connection became ready within 90 seconds. Connect Wi-Fi and use Desktop MAIC PC Start.'
}
function Get-MAICMutexName {
    param([string]$Root)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $digest = $hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant())) }
    finally { $hasher.Dispose() }
    'Local\MAICPC-' + ([BitConverter]::ToString($digest).Replace('-', '').Substring(0, 24))
}
function Test-MAICProcess {
    param($Process, [string]$PythonPath, [string]$ServerPath, $Record = $null)
    $addressPattern = '(?:\d{1,3}\.){3}\d{1,3}'
    $commandPattern = '^"?' + [regex]::Escape($PythonPath) + '"?\s+-X\s+utf8\s+"?' +
        [regex]::Escape($ServerPath) + '"?\s+--host\s+' + $addressPattern +
        '\s+--port\s+8840\s+--network\s+' + $addressPattern + '/\d{1,2}\s*$'
    if (!$Process -or $Process.ExecutablePath -ne $PythonPath -or !$Process.CommandLine -or
        $Process.CommandLine -notmatch $commandPattern) { return $false }
    if ($Record) {
        $live = Get-Process -Id $Process.ProcessId -ErrorAction SilentlyContinue
        if (!$live -or $live.StartTime.ToUniversalTime().Ticks -ne ([datetime]$Record.startedAt).ToUniversalTime().Ticks) { return $false }
    }
    return $true
}
Export-ModuleMember -Function Get-MAICPythonPath, Wait-MAICNetwork, Get-MAICMutexName, Test-MAICProcess
