[CmdletBinding()]
param([string]$Root = 'C:\dsh-lab', [string]$Repository, [string]$AuthorizationFile, [switch]$Execute)
. (Join-Path $PSScriptRoot 'Common.ps1')
$labRoot = Resolve-LabRoot $Root
$folders = @('controller', 'runner', 'work', 'state', 'logs', 'dsh-home', 'tools')
if (-not $Execute) {
    [pscustomobject]@{ Mode='PLAN_ONLY'; Root=$labRoot; Folders=$folders; Sends=$false; Installs=$false } | ConvertTo-Json
    return
}
Assert-LabDeploymentAuthorization -AuthorizationFile $AuthorizationFile -Repository $Repository
foreach ($folder in $folders) { New-Item -ItemType Directory -Force -Path (Join-Path $labRoot $folder) | Out-Null }
Write-Output "Prepared directories under $labRoot. No runner or model was started."
