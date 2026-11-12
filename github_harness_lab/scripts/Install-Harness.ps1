[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$')][string]$Version,
      [string]$Root='C:\dsh-lab\tools', [string]$Repository, [string]$AuthorizationFile, [switch]$Execute)
. (Join-Path $PSScriptRoot 'Common.ps1')
$toolsRoot = Resolve-LabRoot $Root
$package = "@deepseek-ai/dsh@$Version"
if (-not $Execute) {
    [pscustomobject]@{ Mode='PLAN_ONLY'; Package=$package; Root=$toolsRoot; LifecycleScripts='disabled'; StartsModel=$false; CliVerification='manual help/version required' } | ConvertTo-Json
    return
}
Assert-LabDeploymentAuthorization -AuthorizationFile $AuthorizationFile -Repository $Repository
$npm = Get-Command npm.cmd -ErrorAction Stop
if ((Test-Path -LiteralPath $toolsRoot) -and @(Get-ChildItem -LiteralPath $toolsRoot -Force).Count -gt 0) { throw 'Tools target must be empty; refusing to change an existing installation.' }
New-Item -ItemType Directory -Force -Path $toolsRoot | Out-Null
# Keep dependency lifecycle scripts disabled until reviewed. A package requiring one stays BLOCKED.
& $npm.Source install --prefix $toolsRoot --no-audit --no-fund --ignore-scripts --save-exact $package
if ($LASTEXITCODE -ne 0) { throw 'Pinned package install failed; do not attempt a model run.' }
Write-Output 'Package files and lockfile created. Verify official JS entrypoint with native node.exe, --version and headless --help before enabling any model call. No CLI was started.'
