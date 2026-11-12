[CmdletBinding()]
param([switch]$Execute)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$cycleParent = Join-Path $env:LOCALAPPDATA 'HarnessGithubSmoke'
$roots = @(0..2 | ForEach-Object { Join-Path $cycleParent "cycle-runner-$_" })
if (-not $Execute) {
    [pscustomobject]@{Mode='PLAN_ONLY';Repository='WangYu0611/chatgpt-deepseek-harness-test-20261028';Roots=$roots;Label='dsh-cycle-20261028';Count=3;Ephemeral=$true;DefaultLabels=$false;Starts=$false;Service=$false} | ConvertTo-Json
    return
}
foreach ($root in $roots) {
    if (-not (Test-Path -LiteralPath (Join-Path $root 'config.cmd') -PathType Leaf)) { throw 'Prepare all three reviewed Runner roots first.' }
    if (Test-Path -LiteralPath (Join-Path $root '.runner')) { throw 'A root is already registered; reconcile without replacing it.' }
}
$secureCycleToken = $null
$cyclePointer = [IntPtr]::Zero
$cycleRegistration = $env:ACTIONS_RUNNER_INPUT_TOKEN
try {
    if (-not $cycleRegistration) {
        $secureCycleToken = Read-Host 'Paste the short-lived repository registration token locally (hidden, never in chat)' -AsSecureString
        $cyclePointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureCycleToken)
        $cycleRegistration = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($cyclePointer)
    }
    for ($index = 0; $index -lt 3; $index++) {
        $env:ACTIONS_RUNNER_INPUT_TOKEN = $cycleRegistration
        & (Join-Path $PSScriptRoot 'Connect-LiveRunner.ps1') -Root $roots[$index] -Name "dsh-cycle-20261028-$index" -Label 'dsh-cycle-20261028' -Execute
    }
} finally {
    Remove-Item Env:ACTIONS_RUNNER_INPUT_TOKEN -ErrorAction SilentlyContinue
    $cycleRegistration = $null
    if ($cyclePointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($cyclePointer) }
    if ($null -ne $secureCycleToken) { $secureCycleToken.Dispose() }
}
Write-Output 'Three one-job ephemeral registrations completed; no listeners or services started. Remove unused registrations when this experiment expires.'
