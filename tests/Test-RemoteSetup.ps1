$ErrorActionPreference = 'Stop'
$taskSetup = Join-Path (Split-Path -Parent $PSScriptRoot) 'Setup-MAIC-Remote.ps1'
$taskTokens = $null
$taskErrors = $null
$taskAst = [Management.Automation.Language.Parser]::ParseFile($taskSetup, [ref]$taskTokens, [ref]$taskErrors)
if ($taskErrors.Count) { throw 'The remote setup script contains PowerShell syntax errors.' }
$taskNames = @('Test-MAICPortConflict', 'Assert-MAICPreservedRoutes')
$taskFunctions = @($taskAst.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $taskNames}, $true))
if ($taskFunctions.Count -ne 2) { throw 'The expected pure route validation functions were not found.' }
# Load only the two pure functions, never the script's network/setup commands.
foreach ($taskFunction in $taskFunctions) { . ([scriptblock]::Create($taskFunction.Extent.Text)) }
$script:taskPassed = 0
function Assert-Passes { param([string]$Name, [scriptblock]$Body); try { & $Body } catch { throw "Test failed: $Name ($($_.Exception.Message))" }; $script:taskPassed++ }
function Assert-Rejects { param([string]$Name, [scriptblock]$Body); $taskRejected = $false; try { & $Body } catch { $taskRejected = $true }; if (!$taskRejected) { throw "Test failed: $Name was accepted." }; $script:taskPassed++ }
function Copy-TestConfig { param($Value); return ($Value | ConvertTo-Json -Depth 30 -Compress | ConvertFrom-Json) }

$taskDNS = 'maic-test.ts.net'
$taskEmpty = '{}' | ConvertFrom-Json
$taskBefore = '{"TCP":{"443":{"HTTPS":true}},"Web":{"maic-test.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8787"}}}},"AllowFunnel":{}}' | ConvertFrom-Json
$taskAfter = '{"TCP":{"443":{"HTTPS":true},"8443":{"HTTPS":true}},"Web":{"maic-test.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8787"}}},"maic-test.ts.net:8443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8842"}}}},"AllowFunnel":{"maic-test.ts.net:8443":true}}' | ConvertFrom-Json
Assert-Passes 'Empty initial configuration' { Test-MAICPortConflict $taskEmpty $taskDNS }
Assert-Passes 'Existing443 preserved while adding8443' { Assert-MAICPreservedRoutes $taskBefore $taskAfter; Test-MAICPortConflict $taskAfter $taskDNS -RequireRoute }
$taskConflict = Copy-TestConfig $taskAfter
$taskConflict.Web.'maic-test.ts.net:8443'.Handlers.'/'.Proxy = 'http://127.0.0.1:8787'
Assert-Rejects 'Conflicting8443 proxy' { Test-MAICPortConflict $taskConflict $taskDNS }
$taskChanged = Copy-TestConfig $taskAfter
$taskChanged.Web.'maic-test.ts.net:443'.Handlers.'/'.Proxy = 'http://127.0.0.1:8842'
Assert-Rejects 'Changed443 proxy' { Assert-MAICPreservedRoutes $taskBefore $taskChanged }
$taskNewPublic = Copy-TestConfig $taskAfter
$taskNewPublic.AllowFunnel | Add-Member -NotePropertyName 'maic-test.ts.net:443' -NotePropertyValue $true
Assert-Rejects 'New unrelated443 Funnel permission' { Assert-MAICPreservedRoutes $taskBefore $taskNewPublic }
$taskMissingTcp = Copy-TestConfig $taskAfter
$taskMissingTcp.TCP.PSObject.Properties.Remove('8443')
Assert-Rejects 'Required8443 TCP missing' { Test-MAICPortConflict $taskMissingTcp $taskDNS -RequireRoute }
$taskMissingWeb = Copy-TestConfig $taskAfter
$taskMissingWeb.Web.PSObject.Properties.Remove('maic-test.ts.net:8443')
Assert-Rejects 'Required8443 Web missing' { Test-MAICPortConflict $taskMissingWeb $taskDNS -RequireRoute }
Assert-Rejects 'Required8443 route absent' { Test-MAICPortConflict $taskEmpty $taskDNS -RequireRoute }
Write-Output (@{ok=$true; testsPassed=$taskPassed; networkMutations=$false; powerShellVersion=$PSVersionTable.PSVersion.ToString()} | ConvertTo-Json -Compress)
