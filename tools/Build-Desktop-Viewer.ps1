param()
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskEsbuild = Join-Path $PSScriptRoot 'esbuild\esbuild.exe'
if (!(Test-Path -LiteralPath $taskEsbuild)) {
    $taskTar = Get-Command tar -ErrorAction Stop
    $taskToolFolder = Split-Path -Parent $taskEsbuild
    New-Item -ItemType Directory -Path $taskToolFolder -Force | Out-Null
    $taskArchive = Join-Path $taskToolFolder 'win32-x64-0.25.12.tgz'
    $taskToolUrl = 'https://registry.npmjs.org/@esbuild/win32-x64/-/win32-x64-0.25.12.tgz'
    Invoke-WebRequest -UseBasicParsing -Uri $taskToolUrl -OutFile $taskArchive
    $taskHasher = [Security.Cryptography.SHA512]::Create()
    $taskStream = [IO.File]::OpenRead($taskArchive)
    try { $taskActual = [Convert]::ToBase64String($taskHasher.ComputeHash($taskStream)) }
    finally { $taskStream.Dispose(); $taskHasher.Dispose() }
    if ($taskActual -ne 'alJC0uCZpTFrSL0CCDjcgleBXPnCrEAhTBILpeAp7M/OFgoqtAetfBzX0xM00MUsVVPpVjlPuMbREqnZCXaTnA==') {
        throw 'The pinned esbuild archive checksum did not match. Nothing was executed.'
    }
    & $taskTar.Source -xzf $taskArchive -C $taskToolFolder package/esbuild.exe
    if ($LASTEXITCODE -ne 0) { throw 'Could not extract the verified esbuild archive.' }
    Copy-Item -LiteralPath (Join-Path $taskToolFolder 'package\esbuild.exe') -Destination $taskEsbuild
}
if ((Get-FileHash -LiteralPath $taskEsbuild -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'cae1bbc86f4df800b01d99e28aea0a154b02243de6797e98f48a9b88a64a7be0') {
    throw 'The esbuild 0.25.12 executable checksum did not match. Nothing was executed.'
}
$taskInput = Join-Path $taskRoot 'web\vendor\novnc\core\rfb.js'
$taskOutput = Join-Path $taskRoot 'web\vendor\novnc\rfb.bundle.js'
$taskBanner = '/*! noVNC 1.4.0, commit 90455eef0692d2e35276fd31286114d0955016b0; copyright the noVNC authors. MPL-2.0. Supplied source: core/ and vendor/. Licenses: LICENSE.txt and docs/LICENSE.*; pako: vendor/pako/LICENSE. Built with esbuild 0.25.12 for Chrome 78. */'
& $taskEsbuild $taskInput --bundle --minify --format=esm --platform=browser --target=chrome78 --legal-comments=inline ('--banner:js=' + $taskBanner) ('--outfile=' + $taskOutput)
if ($LASTEXITCODE -ne 0) { throw 'The desktop viewer bundle could not be built.' }
[ordered]@{ok=$true; noVncVersion='1.4.0'; target='chrome78'; bytes=(Get-Item -LiteralPath $taskOutput).Length;
    sha256=(Get-FileHash -LiteralPath $taskOutput -Algorithm SHA256).Hash.ToLowerInvariant()} | ConvertTo-Json -Compress
