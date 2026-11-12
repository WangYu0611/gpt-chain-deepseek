[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version,
      [Parameter(Mandatory)][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$Sha256,
      [string]$Root = 'C:\dsh-lab\runner', [string]$Repository, [string]$AuthorizationFile, [switch]$Execute)
. (Join-Path $PSScriptRoot 'Common.ps1')
$runnerRoot = Resolve-LabRoot $Root
$url = "https://github.com/actions/runner/releases/download/v$Version/actions-runner-win-x64-$Version.zip"
if (-not $Execute) {
    [pscustomobject]@{ Mode='PLAN_ONLY'; Download=$url; ExpectedSha256=$Sha256; ExtractTo=$runnerRoot; Registers=$false; Starts=$false } | ConvertTo-Json
    return
}
Assert-LabDeploymentAuthorization -AuthorizationFile $AuthorizationFile -Repository $Repository
if ((Test-Path -LiteralPath $runnerRoot) -and @(Get-ChildItem -LiteralPath $runnerRoot -Force).Count -gt 0) { throw 'Runner target must be empty. Existing registration is never replaced.' }
New-Item -ItemType Directory -Force -Path $runnerRoot | Out-Null
$zip = Join-Path $runnerRoot 'runner-download.zip'
Invoke-WebRequest -Uri $url -OutFile $zip
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ine $Sha256) { throw 'Runner checksum mismatch. Archive retained for diagnosis, not extracted.' }
Expand-Archive -LiteralPath $zip -DestinationPath $runnerRoot
Write-Output 'Downloaded and verified runner. Registration and startup are separate authorized steps.'
