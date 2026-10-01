param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'MAIC-Startup.psm1') -Force
$runtime = Join-Path $PSScriptRoot '.runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
# Reinstallation can repair an obsolete saved path; an explicit path also works
# when Explorer or the terminal has a Microsoft Store alias in PATH.
if (!$PythonPath) { $PythonPath = (Get-Command python -ErrorAction Stop).Source }
$python = Get-MAICPythonPath -Root $PSScriptRoot -PythonPath $PythonPath
@{pythonPath=$python} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtime 'launcher.json') -Encoding UTF8
$desktopFolder = Join-Path ([Environment]::GetFolderPath('Desktop')) 'MAIC PC'
$startupFolder = [Environment]::GetFolderPath('Startup')
New-Item -ItemType Directory -Path $desktopFolder -Force | Out-Null
$shell = New-Object -ComObject WScript.Shell
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
try {
    foreach ($entry in @(@{name='Start';action='Start'}, @{name='Stop';action='Stop'}, @{name='Open Dashboard';action='Open'}, @{name='Help';action='Help'})) {
        $shortcut = $shell.CreateShortcut((Join-Path $desktopFolder ($entry.name + '.lnk')))
        $shortcut.TargetPath = $powershell
        $shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'Invoke-MAIC-Shortcut.ps1') + '" -Action ' + $entry.action
        $shortcut.WorkingDirectory = $PSScriptRoot
        $shortcut.WindowStyle = 7
        $shortcut.Description = $entry.name + ' MAIC PC'
        $shortcut.Save()
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
    }
    $shortcut = $shell.CreateShortcut((Join-Path $startupFolder 'MAIC PC.lnk'))
    $shortcut.TargetPath = $powershell
    $shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'Start-MAIC.ps1') + '"'
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.WindowStyle = 7
    $shortcut.Description = 'Start MAIC PC after Windows sign-in'
    $shortcut.Save()
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
} finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
Write-Host "Desktop controls installed: $desktopFolder"
Write-Host 'Current-user Windows login startup installed.'
