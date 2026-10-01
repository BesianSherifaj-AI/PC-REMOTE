param([switch]$StartOnly, [switch]$Stop, [string]$PythonPath)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskTools = Join-Path $taskRoot 'tools\tightvnc'
$taskApplication = Join-Path $taskTools 'application'
$taskExecutable = Join-Path $taskApplication 'tvnserver.exe'
$taskRuntime = Join-Path $taskRoot '.runtime\desktop'
$taskProcessFile = Join-Path $taskRuntime 'process.json'
$taskBridge = Join-Path $taskRoot 'desktop_bridge.py'

function Get-OwnedDesktopProcess {
    if (!(Test-Path -LiteralPath $taskProcessFile)) { return $null }
    $record = Get-Content -LiteralPath $taskProcessFile -Raw | ConvertFrom-Json
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$record.pid)" -ErrorAction SilentlyContinue
    if (!$process) { return $null }
    $live = Get-Process -Id ([int]$record.pid) -ErrorAction SilentlyContinue
    if (!$live -or $live.HasExited) { return $null }
    if ($process.ExecutablePath -ne $taskExecutable -or $record.executable -ne $taskExecutable -or
        $process.CommandLine -notmatch '(?i)(?:\s|^)-run(?:\s|$)' -or
        $live.StartTime.ToUniversalTime().Ticks -ne ([datetime]$record.startedAt).ToUniversalTime().Ticks) {
        throw 'The saved desktop PID belongs to another process; it will not be stopped or reused.'
    }
    return $process
}

if ($Stop) {
    $taskOwned = Get-OwnedDesktopProcess
    if ($taskOwned) {
        $taskStopping = Get-Process -Id $taskOwned.ProcessId -ErrorAction Stop
        Stop-Process -Id $taskOwned.ProcessId -ErrorAction Stop
        if (!$taskStopping.WaitForExit(5000)) { throw 'The owned desktop process did not stop.' }
        for ($taskAttempt = 0; $taskAttempt -lt 15; $taskAttempt++) {
            if (!(Get-NetTCPConnection -State Listen -OwningProcess $taskOwned.ProcessId -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 100
        }
    }
    if (Test-Path -LiteralPath $taskProcessFile) { Remove-Item -LiteralPath $taskProcessFile }
    Write-Output '{"ok":true,"desktopStopped":true}'
    exit 0
}

if (!$PythonPath) {
    Import-Module (Join-Path $PSScriptRoot 'MAIC-Startup.psm1') -Force
    $PythonPath = Get-MAICPythonPath -Root $PSScriptRoot
}
foreach ($taskDirectory in @($taskTools, $taskApplication, $taskRuntime)) {
    New-Item -ItemType Directory -Path $taskDirectory -Force | Out-Null
}
# Limit the DPAPI credential and process record directory to the current user.
$taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().User
$taskAcl = [Security.AccessControl.DirectorySecurity]::new()
# Copy only the DACL: writing the SACL would require SeSecurityPrivilege.
$taskAcl.SetSecurityDescriptorSddlForm((Get-Acl -LiteralPath $taskRuntime).GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access), [Security.AccessControl.AccessControlSections]::Access)
$taskAcl.SetAccessRuleProtection($true, $false)
foreach ($taskRule in @($taskAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) {
    $taskAcl.RemoveAccessRuleSpecific($taskRule)
}
$taskAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($taskIdentity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    # Windows PowerShell 5.1 has the .NET Framework instance method.
    ([IO.DirectoryInfo]::new($taskRuntime)).SetAccessControl($taskAcl)
} else {
    [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($taskRuntime), $taskAcl)
}

if (!$StartOnly) {
    $taskMsi = Join-Path $taskTools 'tightvnc-2.8.88-gpl-setup-64bit.msi'
    $taskMsiUrl = 'https://www.tightvnc.com/download/2.8.88/tightvnc-2.8.88-gpl-setup-64bit.msi'
    if (!(Test-Path -LiteralPath $taskMsi)) { Invoke-WebRequest -UseBasicParsing -Uri $taskMsiUrl -OutFile $taskMsi }
    $taskMsiHash = (Get-FileHash -LiteralPath $taskMsi -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($taskMsiHash -ne 'fa86d817ac29c5ffe1e8e7095e738d9ba5ca28aa62304ac234580916622a8ca2') { throw 'TightVNC package checksum mismatch.' }
    $taskSignature = Get-AuthenticodeSignature -LiteralPath $taskMsi
    if ($taskSignature.Status -ne 'Valid' -or $taskSignature.SignerCertificate.Thumbprint -ne '90513AC184484A36A6461CB7B834B68761761006') {
        throw 'TightVNC package publisher verification failed.'
    }
    # Read/extract the signed archive without running Windows Installer, its
    # custom actions, service registration or firewall configuration.
    $task7Zip = Join-Path $env:ProgramFiles '7-Zip\7z.exe'
    if (!(Test-Path -LiteralPath $task7Zip)) {
        $task7Zip = (Get-Command 7z -ErrorAction Stop).Source
    }
    $taskExtracted = Join-Path $taskTools 'extracted'
    New-Item -ItemType Directory -Path $taskExtracted -Force | Out-Null
    & $task7Zip x -y ('-o' + $taskExtracted) $taskMsi | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'TightVNC archive extraction failed.' }
    $taskInstaller = New-Object -ComObject WindowsInstaller.Installer
    $taskDatabase = $taskInstaller.OpenDatabase($taskMsi, 0) # Read-only metadata.
    $taskView = $taskDatabase.OpenView('SELECT `File`, `FileName` FROM `File`')
    try {
        $taskView.Execute()
        while ($taskRecord = $taskView.Fetch()) {
            $taskSourceName = $taskRecord.StringData(1)
            $taskTargetName = ($taskRecord.StringData(2) -split '\|')[-1]
            if ($taskTargetName -match '[\\/]' -or $taskTargetName -in @('.', '..')) { throw 'Unsafe package file name.' }
            $taskSourceFile = Join-Path $taskExtracted $taskSourceName
            $taskTargetFile = Join-Path $taskApplication $taskTargetName
            if (!(Test-Path -LiteralPath $taskTargetFile) -or
                (Get-FileHash -LiteralPath $taskSourceFile -Algorithm SHA256).Hash -ne
                (Get-FileHash -LiteralPath $taskTargetFile -Algorithm SHA256).Hash) {
                Copy-Item -LiteralPath $taskSourceFile -Destination $taskTargetFile -Force
            }
        }
    } finally {
        $taskView.Close()
        foreach ($taskObject in @($taskView, $taskDatabase, $taskInstaller)) {
            [Runtime.InteropServices.Marshal]::ReleaseComObject($taskObject) | Out-Null
        }
    }

    $taskCommit = '90455eef0692d2e35276fd31286114d0955016b0'
    $taskNoVncUrl = 'https://codeload.github.com/novnc/noVNC/zip/' + $taskCommit
    $taskNoVncArchive = Join-Path $taskTools 'novnc-1.4.0-90455eef.zip'
    if (!(Test-Path -LiteralPath $taskNoVncArchive)) { Invoke-WebRequest -UseBasicParsing -Uri $taskNoVncUrl -OutFile $taskNoVncArchive }
    $taskNoVncHash = (Get-FileHash -LiteralPath $taskNoVncArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($taskNoVncHash -ne '617d0180df8a0e1053a175147dab8ca48507e2f1694d0441d1e35c1eeef7f203') { throw 'Pinned noVNC source checksum mismatch.' }
    $taskNoVncUnpacked = Join-Path $taskTools 'novnc-source'
    Expand-Archive -LiteralPath $taskNoVncArchive -DestinationPath $taskNoVncUnpacked -Force
    $taskNoVncSource = Join-Path $taskNoVncUnpacked ('noVNC-' + $taskCommit)
    $taskNoVncDestination = Join-Path $taskRoot 'web\vendor\novnc'
    New-Item -ItemType Directory -Path $taskNoVncDestination -Force | Out-Null
    foreach ($taskItem in Get-ChildItem -LiteralPath $taskNoVncSource -Force) {
        Copy-Item -LiteralPath $taskItem.FullName -Destination $taskNoVncDestination -Recurse -Force
    }
    [ordered]@{
        tightvncVersion='2.8.88'; tightvncUrl=$taskMsiUrl; tightvncSha256=$taskMsiHash;
        signatureValid=$true; signer='OOO GlavSoft'; extraction='archive-only; no installer execution';
        noVncVersion='1.4.0'; noVncCommit=$taskCommit; noVncUrl=$taskNoVncUrl; noVncSha256=$taskNoVncHash
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskTools 'provenance.json') -Encoding UTF8
}

if (!(Test-Path -LiteralPath $taskExecutable) -or !(Test-Path -LiteralPath (Join-Path $taskRoot 'web\vendor\novnc\core\rfb.js'))) {
    throw 'Run Setup-Desktop-Viewer.ps1 without -StartOnly once to prepare the desktop files.'
}
$taskOwned = Get-OwnedDesktopProcess
if (!$taskOwned) {
    $taskOther = @(Get-CimInstance Win32_Process -Filter "Name='tvnserver.exe'" | Where-Object {
        $taskLiveProcess = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
        $taskLiveProcess -and !$taskLiveProcess.HasExited
    })
    $taskPort = @(Get-NetTCPConnection -LocalPort 5900 -State Listen -ErrorAction SilentlyContinue)
    if ($taskOther.Count -or $taskPort.Count) { throw 'An existing desktop server or port 5900 listener was found; it will not be changed.' }
    & $PythonPath $taskBridge --configure-vnc
    if ($LASTEXITCODE -ne 0) { throw 'Private desktop configuration failed.' }
    $taskStarted = Start-Process -FilePath $taskExecutable -ArgumentList '-run' -WindowStyle Hidden -PassThru
    [ordered]@{pid=$taskStarted.Id; executable=$taskExecutable; startedAt=$taskStarted.StartTime.ToUniversalTime().ToString('o')} |
        ConvertTo-Json | Set-Content -LiteralPath $taskProcessFile -Encoding UTF8
    $taskOwned = Get-OwnedDesktopProcess
}
try {
    $taskListeners = @()
    for ($taskAttempt = 0; $taskAttempt -lt 25; $taskAttempt++) {
        $taskListeners = @(Get-NetTCPConnection -State Listen -OwningProcess $taskOwned.ProcessId -ErrorAction SilentlyContinue)
        if ($taskListeners.Count) { break }
        Start-Sleep -Milliseconds 200
    }
    if (!$taskListeners.Count -or @($taskListeners | Where-Object { $_.LocalPort -ne 5900 -or $_.LocalAddress -notin @('127.0.0.1','::1') }).Count) {
        throw 'Desktop listener verification failed; a loopback-only port 5900 listener is required.'
    }
    & $PythonPath $taskBridge --verify-rfb
    if ($LASTEXITCODE -ne 0) { throw 'Authenticated desktop protocol verification failed.' }
    Write-Output '{"ok":true,"desktopRunning":true,"loopbackOnly":true,"vncAuthentication":true,"mode":"user-application"}'
} catch {
    $taskSafeToStop = Get-OwnedDesktopProcess
    if ($taskSafeToStop) { Stop-Process -Id $taskSafeToStop.ProcessId -ErrorAction SilentlyContinue }
    throw
}
