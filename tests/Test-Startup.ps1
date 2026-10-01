param([switch]$SkipShortcuts)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskModulePath = Join-Path $taskRoot 'MAIC-Startup.psm1'
$taskModule = Import-Module $taskModulePath -Force -PassThru
$script:taskFailures = @()
$script:taskPassed = 0

function Assert-MAIC {
    param([bool]$Condition, [string]$Message)
    if (!$Condition) { throw $Message }
}
function Test-MAICCase {
    param([string]$Name, [scriptblock]$Action)
    try { & $Action; $script:taskPassed++; Write-Host ('PASS: ' + $Name) }
    catch { $script:taskFailures += $Name; Write-Host ('FAIL: ' + $Name + ' - ' + $_.Exception.Message) }
}
function Assert-MAICThrows {
    param([scriptblock]$Action, [string]$MessagePattern)
    $caught = $false
    try { & $Action | Out-Null }
    catch { $caught = $_.Exception.Message -like $MessagePattern }
    Assert-MAIC $caught ('Expected refusal: ' + $MessagePattern)
}
function New-MAICNetwork {
    param([string]$Alias = 'Wi-Fi', [string]$Address = '192.168.0.20')
    [pscustomobject]@{ InterfaceAlias = $Alias; IPv4DefaultGateway = @([pscustomobject]@{ NextHop = '192.168.0.1' });
                      IPv4Address = @([pscustomobject]@{ IPAddress = $Address; PrefixLength = 24 }) }
}
function Set-MAICLiveMock {
    param($Live)
    & $taskModule {
        param($Mock)
        $script:MAICTestLive = $Mock
        $script:MAICTestSeenId = $null
        function script:Get-Process {
            param([int]$Id, $ErrorAction)
            $script:MAICTestSeenId = $Id
            $script:MAICTestLive
        }
    } $Live
}
function Set-MAICPythonMock {
    param([string]$Path)
    & $taskModule {
        param($MockPath)
        $script:MAICTestPythonPath = $MockPath
        $script:MAICTestPythonReads = 0
        function script:Get-Command {
            param([string]$Name, $ErrorAction)
            $script:MAICTestPythonReads++
            [pscustomobject]@{ Source = $script:MAICTestPythonPath }
        }
    } $Path
}
$taskFixture = Join-Path ([IO.Path]::GetTempPath()) ('MAIC-StartupTests-' + [guid]::NewGuid().ToString('N'))
$taskFixtureRuntime = Join-Path $taskFixture '.runtime'
$taskFixturePythonDirectory = Join-Path $taskFixture 'Python Suite'
$taskFixtureAliasDirectory = Join-Path $taskFixture 'WindowsApps'
$taskFixturePython = Join-Path $taskFixturePythonDirectory 'python.exe'
$taskFixtureAlias = Join-Path $taskFixtureAliasDirectory 'python.exe'
$taskFixtureConfig = Join-Path $taskFixtureRuntime 'launcher.json'

try {
    Test-MAICCase 'Start, Stop, Install, shortcut wrapper and viewer scripts parse without execution' {
        foreach ($name in @('MAIC-Startup.psm1', 'Start-MAIC.ps1', 'Stop-MAIC.ps1', 'Install-MAIC-Desktop.ps1', 'Install-PCRemote.ps1', 'Open-MAIC.ps1', 'Invoke-MAIC-Shortcut.ps1', 'Setup-Desktop-Viewer.ps1')) {
            $taskFile = Join-Path $taskRoot $name
            Assert-MAIC (Test-Path -LiteralPath $taskFile -PathType Leaf) ('Missing script: ' + $name)
            $taskTokens = $null; $taskParseErrors = $null
            [void][Management.Automation.Language.Parser]::ParseFile($taskFile, [ref]$taskTokens, [ref]$taskParseErrors)
            Assert-MAIC (@($taskParseErrors).Count -eq 0) ('Parse errors: ' + $name)
        }
    }

    foreach ($directory in @($taskFixture, $taskFixtureRuntime, $taskFixturePythonDirectory, $taskFixtureAliasDirectory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    # Resolver tests never execute these fixture files or touch the live config.
    [IO.File]::WriteAllText($taskFixturePython, 'fixture only')
    [IO.File]::WriteAllText($taskFixtureAlias, 'fixture alias only')
    Test-MAICCase 'Configured absolute Python with spaces wins over inherited Explorer PATH' {
        Set-MAICPythonMock $taskFixtureAlias
        @{ pythonPath = $taskFixturePython } | ConvertTo-Json | Set-Content -LiteralPath $taskFixtureConfig -Encoding UTF8
        Assert-MAIC ((Get-MAICPythonPath -Root $taskFixture) -eq $taskFixturePython) 'The configured executable was replaced by PATH lookup.'
    }
    Test-MAICCase 'Relative, drive-relative and rooted-without-drive Python config is refused' {
        foreach ($invalidPath in @('python.exe', '.\python.exe', 'C:python.exe', '\python.exe')) {
            @{ pythonPath = $invalidPath } | ConvertTo-Json | Set-Content -LiteralPath $taskFixtureConfig -Encoding UTF8
            Assert-MAICThrows { Get-MAICPythonPath -Root $taskFixture } '*'
        }
    }
    Test-MAICCase 'Missing executable and directory configured as Python are refused' {
        foreach ($invalidPath in @((Join-Path $taskFixturePythonDirectory 'missing.exe'), $taskFixturePythonDirectory)) {
            @{ pythonPath = $invalidPath } | ConvertTo-Json | Set-Content -LiteralPath $taskFixtureConfig -Encoding UTF8
            Assert-MAICThrows { Get-MAICPythonPath -Root $taskFixture } '*'
        }
    }
    Test-MAICCase 'Existing WindowsApps alias is refused even when configured explicitly' {
        @{ pythonPath = $taskFixtureAlias } | ConvertTo-Json | Set-Content -LiteralPath $taskFixtureConfig -Encoding UTF8
        Assert-MAICThrows { Get-MAICPythonPath -Root $taskFixture } '*'
    }
    Test-MAICCase 'Malformed or incomplete Python config never falls back to PATH' {
        Set-MAICPythonMock $taskFixturePython
        foreach ($invalidJson in @('{', '{}', '{"pythonPath":""}')) {
            [IO.File]::WriteAllText($taskFixtureConfig, $invalidJson)
            Assert-MAICThrows { Get-MAICPythonPath -Root $taskFixture } '*'
        }
    }
    Test-MAICCase 'Explicit verified Python path can repair stale or malformed saved config' {
        [IO.File]::WriteAllText($taskFixtureConfig, '{')
        Set-MAICPythonMock $taskFixtureAlias
        Assert-MAIC ((Get-MAICPythonPath -Root $taskFixture -PythonPath $taskFixturePython) -eq $taskFixturePython) 'An explicit repair path still depended on the damaged saved config or PATH.'
        Assert-MAICThrows { Get-MAICPythonPath -Root $taskFixture -PythonPath $taskFixtureAlias } '*'
    }
    Test-MAICCase 'Absent Python config uses a verified PATH executable and rejects aliases' {
        Remove-Item -LiteralPath $taskFixtureConfig
        Set-MAICPythonMock $taskFixturePython
        Assert-MAIC ((Get-MAICPythonPath -Root $taskFixture) -eq $taskFixturePython) 'No-config fallback did not select the verified PATH executable.'
        $reads = & $taskModule { $script:MAICTestPythonReads }
        Assert-MAIC ($reads -gt 0) 'No-config fallback did not query PATH.'
        Set-MAICPythonMock $taskFixtureAlias
        Assert-MAICThrows { Get-MAICPythonPath -Root $taskFixture } '*'
        Set-MAICPythonMock (Join-Path $taskFixturePythonDirectory 'missing.exe')
        Assert-MAICThrows { Get-MAICPythonPath -Root $taskFixture } '*'
    }
    & $taskModule { Remove-Item -LiteralPath 'Function:Get-Command' -ErrorAction SilentlyContinue }

    Test-MAICCase 'Absent Wi-Fi waits exactly 90 simulated seconds, then refuses' {
        $state = @{ Elapsed = 0; Reads = 0; Pauses = 0 }
        $read = { $state.Reads++; @() }.GetNewClosure()
        $pause = { param($Seconds) $state.Elapsed += $Seconds; $state.Pauses++ }.GetNewClosure()
        Assert-MAICThrows { Wait-MAICNetwork -TimeoutSeconds 90 -ReadNetwork $read -Pause $pause } '*No LAN connection*90 seconds*'
        Assert-MAIC ($state.Elapsed -eq 90 -and $state.Reads -eq 46 -and $state.Pauses -eq 45) 'Wi-Fi timeout exceeded or skipped the 90-second boundary.'
    }

    Test-MAICCase 'Wi-Fi readiness at 0, 2, 30, 88 and 90 seconds is accepted' {
        foreach ($delay in @(0, 2, 30, 88, 90)) {
            $ready = New-MAICNetwork
            $state = @{ Elapsed = 0; Reads = 0; Pauses = 0 }
            $read = { $state.Reads++; if ($state.Elapsed -ge $delay) { $ready } }.GetNewClosure()
            $pause = { param($Seconds) $state.Elapsed += $Seconds; $state.Pauses++ }.GetNewClosure()
            $found = Wait-MAICNetwork -TimeoutSeconds 90 -ReadNetwork $read -Pause $pause
            Assert-MAIC ($found.InterfaceAlias -eq 'Wi-Fi') 'Delayed Wi-Fi was not selected.'
            Assert-MAIC ($state.Elapsed -eq $delay) ('Incorrect simulated delay: ' + $delay)
        }
    }

    Test-MAICCase 'Multiple LAN gateways refuse immediately' {
        $connections = @((New-MAICNetwork), (New-MAICNetwork -Alias 'Ethernet' -Address '192.168.1.20'))
        $state = @{ Pauses = 0 }
        $read = { $connections }.GetNewClosure()
        $pause = { param($Seconds) $state.Pauses++ }.GetNewClosure()
        Assert-MAICThrows { Wait-MAICNetwork -ReadNetwork $read -Pause $pause } '*More than one LAN connection*'
        Assert-MAIC ($state.Pauses -eq 0) 'Multiple networks unexpectedly waited.'
    }

    Test-MAICCase 'VPN, loopback, link-local and missing gateways are excluded' {
        $noGateway = New-MAICNetwork -Alias 'Unconnected'; $noGateway.IPv4DefaultGateway = @()
        $connections = @((New-MAICNetwork -Alias 'Tailscale'), (New-MAICNetwork -Alias 'Work VPN'),
                         (New-MAICNetwork -Alias 'Loopback'), (New-MAICNetwork -Address '169.254.1.2'),
                         (New-MAICNetwork -Address '127.0.0.1'), $noGateway, (New-MAICNetwork -Alias 'Expected Wi-Fi'))
        $read = { $connections }.GetNewClosure()
        $found = Wait-MAICNetwork -ReadNetwork $read -Pause { throw 'A ready LAN should not sleep.' }
        Assert-MAIC ($found.InterfaceAlias -eq 'Expected Wi-Fi') 'An excluded adapter was selected.'
    }

    Test-MAICCase 'Explicit LAN selection chooses exactly that adapter among multiple gateways' {
        $connections = @((New-MAICNetwork), (New-MAICNetwork -Alias 'Ethernet' -Address '192.168.1.20'))
        $read = { $connections }.GetNewClosure()
        $found = Wait-MAICNetwork -InterfaceAlias 'Ethernet' -ReadNetwork $read -Pause { throw 'The chosen LAN is already ready.' }
        Assert-MAIC ($found.InterfaceAlias -eq 'Ethernet') 'Explicit adapter choice was ignored.'
    }

    Test-MAICCase 'A missing selected adapter never silently falls back to another network' {
        $connections = @(New-MAICNetwork -Alias 'Ethernet')
        $read = { $connections }.GetNewClosure()
        Assert-MAICThrows { Wait-MAICNetwork -InterfaceAlias 'Wi-Fi' -TimeoutSeconds 0 -ReadNetwork $read } '*No LAN connection*'
        $connections = @(New-MAICNetwork -Alias 'Work VPN')
        $read = { $connections }.GetNewClosure()
        Assert-MAICThrows { Wait-MAICNetwork -InterfaceAlias 'Work VPN' -TimeoutSeconds 0 -ReadNetwork $read } '*No LAN connection*'
    }

    Test-MAICCase 'Mutex identity is stable, case-insensitive and scoped to the root' {
        $first = Get-MAICMutexName 'C:\MAIC Test'
        Assert-MAIC ($first -eq (Get-MAICMutexName 'c:\maic test')) 'Root casing changes mutex identity.'
        Assert-MAIC ($first -eq (Get-MAICMutexName 'C:\MAIC Test')) 'Mutex identity is not stable.'
        Assert-MAIC ($first -ne (Get-MAICMutexName 'C:\Other MAIC')) 'Different workspaces share a mutex.'
        Assert-MAIC ($first -match '^Local\\MAICPC-[0-9A-F]{24}$') 'Mutex format changed.'
    }

    $python = 'C:\Python\python.exe'
    $server = 'C:\MAIC Test\maic_server.py'
    $command = '"' + $python + '" -X utf8 "' + $server + '" --host 192.168.0.20 --port 8840 --network 192.168.0.20/24'
    $process = [pscustomobject]@{ ProcessId = 424242; ExecutablePath = $python; CommandLine = $command }
    $started = [datetime]::SpecifyKind([datetime]'2026-09-30T06:15:23.1234567', [DateTimeKind]::Utc)
    $record = [pscustomobject]@{ pid = 424242; executable = $python; server = $server; startedAt = $started.ToString('o') }

    Test-MAICCase 'Process ownership requires the expected executable and exact script path' {
        Assert-MAIC (Test-MAICProcess $process $python $server) 'Exact owned process was refused.'
        Assert-MAIC (!(Test-MAICProcess $null $python $server)) 'Missing process was accepted.'
        $other = [pscustomobject]@{ ProcessId = 424242; ExecutablePath = 'C:\Other\python.exe'; CommandLine = $command }
        Assert-MAIC (!(Test-MAICProcess $other $python $server)) 'Different Python executable was accepted.'
        $other.ExecutablePath = $python
        $other.CommandLine = '"' + $python + '" "' + $server + '.backup"'
        Assert-MAIC (!(Test-MAICProcess $other $python $server)) 'A path suffix was accepted as the server.'
        $other.CommandLine = $null
        Assert-MAIC (!(Test-MAICProcess $other $python $server)) 'An inaccessible command line was accepted.'
    }

    Test-MAICCase 'Mentioning the server path as another program argument is not ownership' {
        $other = [pscustomobject]@{ ProcessId = 424242; ExecutablePath = $python;
            CommandLine = ('"' + $python + '" "C:\Other\other.py" "' + $server + '"') }
        Assert-MAIC (!(Test-MAICProcess $other $python $server)) 'An unrelated Python script mentioning the server path was accepted.'
        $other.CommandLine = '"' + $python + '" -c "print(''' + $server + ''')"'
        Assert-MAIC (!(Test-MAICProcess $other $python $server)) 'Python inline code mentioning the server path was accepted.'
    }

    Test-MAICCase 'Timestamp records identify the owned PID and reject PID reuse' {
        Set-MAICLiveMock ([pscustomobject]@{ Id = 424242; StartTime = $started })
        Assert-MAIC (Test-MAICProcess $process $python $server $record) 'Matching PID timestamp was refused.'
        $seen = & $taskModule { $script:MAICTestSeenId }
        Assert-MAIC ($seen -eq 424242) 'Ownership checked a different PID.'
        Set-MAICLiveMock ([pscustomobject]@{ Id = 424242; StartTime = $started.AddTicks(1) })
        Assert-MAIC (!(Test-MAICProcess $process $python $server $record)) 'Reused PID with a different start timestamp was accepted.'
        Set-MAICLiveMock $null
        Assert-MAIC (!(Test-MAICProcess $process $python $server $record)) 'A vanished PID was accepted.'
    }

    Test-MAICCase 'Viewer ACL calls use types available in the shortcut runtime' {
        $taskTokens = $null; $taskParseErrors = $null
        $viewerAst = [Management.Automation.Language.Parser]::ParseFile((Join-Path $taskRoot 'Setup-Desktop-Viewer.ps1'), [ref]$taskTokens, [ref]$taskParseErrors)
        $guards = @($viewerAst.FindAll({
            param($Node)
            $Node -is [Management.Automation.Language.IfStatementAst] -and
                $Node.Clauses[0].Item1.Extent.Text -match '\$PSVersionTable\.PSEdition\s+-eq\s+[''"]Desktop[''"]'
        }, $true))
        Assert-MAIC ($guards.Count -eq 1) 'Viewer ACL needs one explicit Desktop/Core runtime guard.'
        $desktopBranch = $guards[0].Clauses[0].Item2
        $coreBranch = $guards[0].ElseClause
        Assert-MAIC ($null -ne $coreBranch) 'Viewer ACL runtime guard has no Core branch.'
        $extensionReferences = @($viewerAst.FindAll({
            param($Node)
            $Node -is [Management.Automation.Language.TypeExpressionAst] -and
                $Node.TypeName.FullName -match '^(System\.)?IO\.FileSystemAclExtensions$'
        }, $true))
        foreach ($reference in $extensionReferences) {
            Assert-MAIC ($reference.Extent.StartOffset -ge $coreBranch.Extent.StartOffset -and
                        $reference.Extent.EndOffset -le $coreBranch.Extent.EndOffset) 'FileSystemAclExtensions is referenced outside the guarded Core branch.'
        }
        if ($PSVersionTable.PSEdition -eq 'Desktop') {
            $methods = @([IO.DirectoryInfo].GetMethods() | Where-Object { $_.Name -eq 'SetAccessControl' })
            Assert-MAIC ($methods.Count -gt 0) '.NET Framework DirectoryInfo.SetAccessControl is unavailable.'
            Assert-MAIC ($desktopBranch.Extent.Text -match '\.SetAccessControl\s*\(') 'Desktop ACL branch does not use the Framework instance method.'
        } else {
            $extensionType = 'System.IO.FileSystemAclExtensions' -as [type]
            Assert-MAIC ($null -ne $extensionType) 'FileSystemAclExtensions is unavailable in the Core runtime.'
            $methods = @($extensionType.GetMethods() | Where-Object { $_.Name -eq 'SetAccessControl' })
            Assert-MAIC ($methods.Count -gt 0 -and $extensionReferences.Count -gt 0) 'Core ACL branch does not use an available SetAccessControl extension.'
        }
    }

    if (!$SkipShortcuts) {
        Test-MAICCase 'Installed current-user desktop shortcuts have exact targets and arguments' {
            $folder = Join-Path ([Environment]::GetFolderPath('Desktop')) 'MAIC PC'
            $expectedTarget = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
            $shell = New-Object -ComObject WScript.Shell
            try {
                foreach ($entry in @(@{ Name = 'Start'; Action = 'Start' }, @{ Name = 'Stop'; Action = 'Stop' }, @{ Name = 'Open Dashboard'; Action = 'Open' }, @{ Name = 'Help'; Action = 'Help' })) {
                    $path = Join-Path $folder ($entry.Name + '.lnk')
                    Assert-MAIC (Test-Path -LiteralPath $path -PathType Leaf) ('Missing desktop shortcut: ' + $entry.Name)
                    $shortcut = $shell.CreateShortcut($path)
                    try {
                        $expectedArgs = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $taskRoot 'Invoke-MAIC-Shortcut.ps1') + '" -Action ' + $entry.Action
                        Assert-MAIC ($shortcut.TargetPath -eq $expectedTarget) ('Incorrect target: ' + $entry.Name)
                        Assert-MAIC ($shortcut.Arguments -eq $expectedArgs) ('Incorrect arguments: ' + $entry.Name)
                        Assert-MAIC ($shortcut.WorkingDirectory -eq $taskRoot) ('Incorrect working directory: ' + $entry.Name)
                        Assert-MAIC ($shortcut.WindowStyle -eq 7) ('Shortcut is not hidden: ' + $entry.Name)
                    } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut) }
                }
            } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
        }
        Test-MAICCase 'Windows sign-in shortcut belongs to this user and uses hidden Windows PowerShell 5.1' {
            $path = Join-Path ([Environment]::GetFolderPath('Startup')) 'MAIC PC.lnk'
            Assert-MAIC (Test-Path -LiteralPath $path -PathType Leaf) 'Missing current-user sign-in shortcut.'
            $shell = New-Object -ComObject WScript.Shell
            try {
                $shortcut = $shell.CreateShortcut($path)
                try {
                    $expectedArgs = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $taskRoot 'Start-MAIC.ps1') + '"'
                    Assert-MAIC ($shortcut.TargetPath -eq (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')) 'Sign-in shortcut targets a different PowerShell runtime.'
                    Assert-MAIC ($shortcut.Arguments -eq $expectedArgs) 'Sign-in startup arguments are incorrect.'
                    Assert-MAIC ($shortcut.WorkingDirectory -eq $taskRoot -and $shortcut.WindowStyle -eq 7) 'Sign-in startup working directory or hidden window setting is incorrect.'
                } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut) }
            } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
        }
    }
} finally {
    & $taskModule {
        Remove-Item -LiteralPath 'Function:Get-Process' -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath 'Function:Get-Command' -ErrorAction SilentlyContinue
        Remove-Variable -Name MAICTestLive, MAICTestSeenId -Scope Script -ErrorAction SilentlyContinue
        Remove-Variable -Name MAICTestPythonPath, MAICTestPythonReads -Scope Script -ErrorAction SilentlyContinue
    }
    Remove-Module $taskModule -ErrorAction SilentlyContinue
    # Only explicit files/directories created by this test are removed; no
    # recursive deletion or workspace configuration mutation is involved.
    foreach ($file in @($taskFixtureConfig, $taskFixturePython, $taskFixtureAlias)) {
        if (Test-Path -LiteralPath $file -PathType Leaf) { Remove-Item -LiteralPath $file -Force }
    }
    foreach ($directory in @($taskFixtureRuntime, $taskFixturePythonDirectory, $taskFixtureAliasDirectory, $taskFixture)) {
        if (Test-Path -LiteralPath $directory -PathType Container) { Remove-Item -LiteralPath $directory }
    }
}
Write-Host ("Startup tests: {0} passed, {1} failed. Runtime: PowerShell {2}." -f $script:taskPassed, $script:taskFailures.Count, $PSVersionTable.PSVersion)
if ($script:taskFailures.Count) { throw ('Startup test failures: ' + ($script:taskFailures -join ', ')) }
