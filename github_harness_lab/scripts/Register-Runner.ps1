[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')][string]$Repository,
      [string]$Root='C:\dsh-lab\runner', [ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Name='dsh-lab-worker',
      [string]$AuthorizationFile, [switch]$Execute)
. (Join-Path $PSScriptRoot 'Common.ps1')
$runnerRoot = Resolve-LabRoot $Root
if (-not $Execute) {
    [pscustomobject]@{ Mode='PLAN_ONLY'; Repository=$Repository; Root=$runnerRoot; Name=$Name; Labels='dsh-lab'; TokenSource='process environment ACTIONS_RUNNER_INPUT_TOKEN'; InstallsService=$false; Starts=$false } | ConvertTo-Json
    return
}
Assert-LabDeploymentAuthorization -AuthorizationFile $AuthorizationFile -Repository $Repository
if (-not $env:ACTIONS_RUNNER_INPUT_TOKEN) { throw 'Set the short-lived registration token in this process environment without putting it in a command line or file.' }
if (Test-Path -LiteralPath (Join-Path $runnerRoot '.runner')) { throw 'Runner is already registered; refusing replacement.' }
$configScript = Join-Path $runnerRoot 'config.cmd'
if (-not (Test-Path -LiteralPath $configScript -PathType Leaf)) { throw 'Install the checksum-verified runner first.' }
Push-Location $runnerRoot
try {
    # The official runner consumes ACTIONS_RUNNER_INPUT_TOKEN and masks/removes it.
    & $configScript --unattended --url "https://github.com/$Repository" --name $Name --labels dsh-lab --work _work
    if ($LASTEXITCODE -ne 0) { throw 'Runner registration failed.' }
} finally {
    Remove-Item Env:ACTIONS_RUNNER_INPUT_TOKEN -ErrorAction SilentlyContinue
    Pop-Location
}
Write-Output 'Registered only. Runner is not started; service installation requires a separate reviewed admin procedure.'
