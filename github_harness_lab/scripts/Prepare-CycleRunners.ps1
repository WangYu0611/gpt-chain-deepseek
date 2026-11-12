[CmdletBinding()]
param([switch]$Execute)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$cycleParent = Join-Path $env:LOCALAPPDATA 'HarnessGithubSmoke'
$archive = Join-Path $cycleParent 'runner\actions-runner-win-x64-2.338.2.zip'
$expectedDigest = 'a1f17aa987fc8158bc7521d284912267a0d01b7c2c4684b33c86783b5e8221cc'
$roots = @(0..2 | ForEach-Object { Join-Path $cycleParent "cycle-runner-$_" })
if (-not $Execute) {
    [pscustomobject]@{Mode='PLAN_ONLY';Roots=$roots;Archive=$archive;Sha256=$expectedDigest;Version='2.338.2';Registers=$false;Starts=$false;Service=$false} | ConvertTo-Json
    return
}
if (-not (Test-Path -LiteralPath $archive -PathType Leaf) -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ine $expectedDigest) { throw 'Reviewed official Runner archive missing or checksum mismatch.' }
foreach ($root in $roots) {
    if ((Test-Path -LiteralPath $root) -and @(Get-ChildItem -LiteralPath $root -Force).Count -gt 0) { throw 'A destination is nonempty; it will not be replaced.' }
}
foreach ($root in $roots) {
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $root
    $version = (& (Join-Path $root 'bin\Runner.Listener.exe') --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $version -ne '2.338.2') { throw 'Extracted official version differs.' }
}
[pscustomobject]@{Mode='PREPARED';Roots=$roots;Registered=$false;Started=$false} | ConvertTo-Json
