[CmdletBinding()]
param(
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'HarnessGithubSmoke\runner'),
    [switch]$Execute
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$runnerVersion = '2.338.2'
$runnerSha256 = 'a1f17aa987fc8158bc7521d284912267a0d01b7c2c4684b33c86783b5e8221cc'
$runnerUrl = "https://github.com/actions/runner/releases/download/v$runnerVersion/actions-runner-win-x64-$runnerVersion.zip"
$runnerRoot = [IO.Path]::GetFullPath($Root)
if ($runnerRoot.StartsWith('\\') -or $runnerRoot -eq [IO.Path]::GetPathRoot($runnerRoot)) { throw 'Choose a local dedicated subdirectory.' }
if (-not $Execute) {
    [pscustomobject]@{Mode='PLAN_ONLY';Version=$runnerVersion;Download=$runnerUrl;Sha256=$runnerSha256;Root=$runnerRoot;Registers=$false;Starts=$false;InstallsService=$false} | ConvertTo-Json
    return
}
if ((Test-Path -LiteralPath $runnerRoot) -and @(Get-ChildItem -LiteralPath $runnerRoot -Force).Count -gt 0) { throw 'The destination must be empty; existing runners and files are never replaced.' }
New-Item -ItemType Directory -Path $runnerRoot -Force | Out-Null
$runnerArchive = Join-Path $runnerRoot "actions-runner-win-x64-$runnerVersion.zip"
Invoke-WebRequest -Uri $runnerUrl -OutFile $runnerArchive -MaximumRedirection 5
if ((Get-FileHash -LiteralPath $runnerArchive -Algorithm SHA256).Hash -ine $runnerSha256) { throw 'Official runner SHA-256 mismatch; archive retained and not extracted.' }
Expand-Archive -LiteralPath $runnerArchive -DestinationPath $runnerRoot
$runnerListener = Join-Path $runnerRoot 'bin\Runner.Listener.exe'
if (-not (Test-Path -LiteralPath $runnerListener -PathType Leaf)) { throw 'Runner.Listener.exe was not extracted.' }
$versionOutput = (& $runnerListener --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $versionOutput -ne $runnerVersion) { throw 'Extracted runner version check failed.' }
[pscustomobject]@{Mode='PREPARED';Version=$versionOutput;ArchiveSha256=$runnerSha256;Root=$runnerRoot;Registered=$false;Started=$false} | ConvertTo-Json
