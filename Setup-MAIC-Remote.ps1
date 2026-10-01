param([switch]$EnableFunnel, [string]$PythonPath = (Get-Command python -ErrorAction Stop).Source)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskRuntime = Join-Path $taskRoot '.runtime\remote'
$taskTailscale = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
if (!(Test-Path -LiteralPath $taskTailscale)) { throw 'Install and sign in to Tailscale on this PC first.' }

# All CLI arguments below are fixed. Captured output can contain account approval
# links, so it stays in memory and is never emitted or copied into a log.
function Invoke-MAICCaptured {
    param([string]$Executable, [string]$Arguments, [int]$TimeoutSeconds = 20)
    $taskInfo = [Diagnostics.ProcessStartInfo]::new()
    $taskInfo.FileName = $Executable
    $taskInfo.Arguments = $Arguments
    $taskInfo.WorkingDirectory = $taskRoot
    $taskInfo.UseShellExecute = $false
    $taskInfo.CreateNoWindow = $true
    $taskInfo.RedirectStandardOutput = $true
    $taskInfo.RedirectStandardError = $true
    $taskChild = [Diagnostics.Process]::new()
    $taskChild.StartInfo = $taskInfo
    try {
        if (!$taskChild.Start()) { throw 'The remote setup command did not start.' }
        $taskOutput = $taskChild.StandardOutput.ReadToEndAsync()
        $taskError = $taskChild.StandardError.ReadToEndAsync()
        if (!$taskChild.WaitForExit($TimeoutSeconds * 1000)) {
            $taskChild.Kill()
            $taskChild.WaitForExit()
            throw 'The remote setup command timed out. No account approval link was logged.'
        }
        return [pscustomobject]@{ExitCode=$taskChild.ExitCode; Output=$taskOutput.Result; Error=$taskError.Result}
    } finally {
        $taskChild.Dispose()
    }
}

function Get-MAICServeConfig {
    $taskResult = Invoke-MAICCaptured $taskTailscale 'serve status --json'
    if ($taskResult.ExitCode -ne 0) { throw 'Could not read the existing Tailscale Serve configuration.' }
    try { return ($taskResult.Output | ConvertFrom-Json) }
    catch { throw 'Tailscale returned an invalid Serve configuration.' }
}

function Test-MAICPortConflict {
    param($Configuration, [string]$DNSName, [switch]$RequireRoute)
    $taskTcp = @($Configuration.TCP.PSObject.Properties | Where-Object { $_.Name -eq '8443' })
    $taskWeb = @($Configuration.Web.PSObject.Properties | Where-Object { $_.Name -match ':8443$' })
    foreach ($taskEntry in $taskWeb) {
        $taskHandlers = @($taskEntry.Value.Handlers.PSObject.Properties | Where-Object { $_ })
        if ($taskEntry.Name -ne "${DNSName}:8443" -or $taskHandlers.Count -ne 1 -or
            $taskHandlers[0].Name -ne '/' -or $taskHandlers[0].Value.Proxy -ne 'http://127.0.0.1:8842') {
            throw 'Tailscale port 8443 already serves another route. Existing routes were preserved.'
        }
    }
    if ($taskTcp.Count -gt 0 -and (!$taskTcp[0].Value.HTTPS -or $taskWeb.Count -ne 1)) {
        throw 'Tailscale port 8443 has a conflicting listener. Existing routes were preserved.'
    }
    if ($RequireRoute -and ($taskTcp.Count -ne 1 -or $taskWeb.Count -ne 1)) {
        throw 'Tailscale did not create the intended HTTPS 8443 route to this gateway.'
    }
}

function Assert-MAICPreservedRoutes {
    param($Before, $After)
    foreach ($taskSection in @('TCP', 'Web', 'AllowFunnel')) {
        foreach ($taskAdded in @($After.$taskSection.PSObject.Properties | Where-Object { $_ })) {
            if (($taskSection -eq 'TCP' -and $taskAdded.Name -eq '8443') -or
                ($taskSection -ne 'TCP' -and $taskAdded.Name -match ':8443$')) { continue }
            $taskPrevious = @($Before.$taskSection.PSObject.Properties | Where-Object { $_.Name -eq $taskAdded.Name })
            if ($taskPrevious.Count -ne 1) {
                throw 'Tailscale reported a new unrelated route or public port. Inspect Tailscale before using remote access.'
            }
        }
        foreach ($taskEntry in @($Before.$taskSection.PSObject.Properties | Where-Object { $_ })) {
            if (($taskSection -eq 'TCP' -and $taskEntry.Name -eq '8443') -or
                ($taskSection -ne 'TCP' -and $taskEntry.Name -match ':8443$')) { continue }
            $taskCurrent = @($After.$taskSection.PSObject.Properties | Where-Object { $_.Name -eq $taskEntry.Name })
            if ($taskCurrent.Count -ne 1 -or
                ($taskEntry.Value | ConvertTo-Json -Depth 30 -Compress) -ne
                ($taskCurrent[0].Value | ConvertTo-Json -Depth 30 -Compress)) {
                throw 'Tailscale reported an unexpected change to an existing route. Inspect Tailscale on this PC; the saved pre-setup snapshot is available locally.'
            }
        }
    }
}

$taskTests = Invoke-MAICCaptured $PythonPath '-X utf8 -m unittest discover -s tests -p test_remote_access.py -q' 30
if ($taskTests.ExitCode -ne 0) { throw 'Remote gateway tests failed. Funnel was not enabled.' }
$taskStatusResult = Invoke-MAICCaptured $taskTailscale 'status --json'
if ($taskStatusResult.ExitCode -ne 0) { throw 'Tailscale status is unavailable. Sign in on this PC first.' }
try { $taskStatus = $taskStatusResult.Output | ConvertFrom-Json }
catch { throw 'Tailscale status could not be read.' }
if ($taskStatus.BackendState -ne 'Running' -or !$taskStatus.Self.Online) {
    throw 'Tailscale must be signed in and connected on this PC before remote setup.'
}
$taskDNS = ([string]$taskStatus.Self.DNSName).TrimEnd('.').ToLowerInvariant()
if ($taskDNS -notmatch '^[a-z0-9][a-z0-9.-]*\.ts\.net$') {
    throw 'Enable MagicDNS and HTTPS for this Tailscale network before remote setup.'
}
$taskOrigin = "https://${taskDNS}:8443"
$taskBefore = Get-MAICServeConfig
Test-MAICPortConflict $taskBefore $taskDNS
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
$taskConfig = Join-Path $taskRuntime 'config.json'
$taskTemporary = Join-Path $taskRuntime 'config.pending.json'
try {
    [IO.File]::WriteAllText($taskTemporary, (@{publicOrigin=$taskOrigin} | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $taskTemporary -Destination $taskConfig -Force
} finally {
    if (Test-Path -LiteralPath $taskTemporary) { Remove-Item -LiteralPath $taskTemporary }
}
if (!$EnableFunnel) {
    Write-Output (@{ok=$true; configured=$true; funnelEnabled=$false; url=$taskOrigin;
        message='Gateway configuration saved. Restart the MAIC dashboard, then run this script with -EnableFunnel.'} | ConvertTo-Json -Compress)
    exit 0
}

try {
    $taskHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8842/_remote/health' -Headers @{Host="${taskDNS}:8443"} -TimeoutSec 3
} catch { throw 'Start the configured MAIC dashboard and verify its loopback remote gateway before enabling Funnel.' }
if (!$taskHealth.ok -or $taskHealth.service -ne 'MAICRemote' -or !$taskHealth.enabled) {
    throw 'The loopback gateway health check failed. Funnel was not enabled.'
}
# Keep an account-free route snapshot locally. Never reset Serve or change port 443.
[IO.File]::WriteAllText((Join-Path $taskRuntime 'tailscale-before.json'), ($taskBefore | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
$taskEnable = Invoke-MAICCaptured $taskTailscale 'funnel --bg --https=8443 http://127.0.0.1:8842' 30
if ($taskEnable.ExitCode -ne 0) {
    throw 'Funnel could not be enabled. Check Funnel permission and HTTPS in the Tailscale admin console on this PC, then rerun setup. Account approval output was not logged.'
}
$taskAfter = Get-MAICServeConfig
Assert-MAICPreservedRoutes $taskBefore $taskAfter
Test-MAICPortConflict $taskAfter $taskDNS -RequireRoute
$taskPublic = @($taskAfter.AllowFunnel.PSObject.Properties | Where-Object { $_.Name -eq "${taskDNS}:8443" -and $_.Value -eq $true })
if ($taskPublic.Count -ne 1) { throw 'Funnel has not reported the intended HTTPS route as enabled.' }
Write-Output (@{ok=$true; configured=$true; funnelEnabled=$true; url=$taskOrigin;
    message='HTTPS route enabled. Approve each browser from the Remote access card on this PC.'} | ConvertTo-Json -Compress)
