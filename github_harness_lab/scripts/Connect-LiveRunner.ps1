[CmdletBinding()]
param(
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'HarnessGithubSmoke\runner'),
    [ValidatePattern('^[A-Za-z0-9_.-]{1,64}$')][string]$Name = 'dsh-live-smoke-20261028',
    [ValidateSet('dsh-smoke-20261028', 'dsh-cycle-20261028')][string]$Label = 'dsh-smoke-20261028',
    [switch]$Execute,
    [switch]$Start
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repository = 'WangYu0611/chatgpt-deepseek-harness-test-20261028'
$runnerRoot = [IO.Path]::GetFullPath($Root)
if (-not $Execute) {
    [pscustomobject]@{Mode='PLAN_ONLY';Repository=$repository;Root=$runnerRoot;Name=$Name;Labels=$Label;DefaultLabels=$false;Ephemeral=$true;TokenSource='Hidden local Read-Host prompt or ACTIONS_RUNNER_INPUT_TOKEN';InstallsService=$false;Starts=[bool]$Start} | ConvertTo-Json
    return
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -or (@($identity.Groups | ForEach-Object {$_.Value}) -contains 'S-1-5-32-544')) { throw 'Use a standard non-administrator account for the live runner.' }
if (Test-Path -LiteralPath (Join-Path $runnerRoot '.runner')) { throw 'Already configured. This script never replaces a runner.' }
$configScript = Join-Path $runnerRoot 'config.cmd'
$runScript = Join-Path $runnerRoot 'run.cmd'
if (-not (Test-Path -LiteralPath $configScript -PathType Leaf) -or -not (Test-Path -LiteralPath $runScript -PathType Leaf)) { throw 'Run Prepare-LiveRunner.ps1 first.' }
$secureRegistration = $null
$registrationPointer = [IntPtr]::Zero
Push-Location $runnerRoot
try {
    if (-not $env:ACTIONS_RUNNER_INPUT_TOKEN) {
        $secureRegistration = Read-Host 'Paste this repository one-hour runner registration token locally (hidden; do not send it in chat)' -AsSecureString
        $registrationPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureRegistration)
        $env:ACTIONS_RUNNER_INPUT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($registrationPointer)
    }
    if (-not $env:ACTIONS_RUNNER_INPUT_TOKEN) { throw 'A nonempty registration token is required.' }
    # Official runner consumes, masks, and removes ACTIONS_RUNNER_INPUT_* variables.
    # No token is passed on the command line, printed, or written by this script.
    & $configScript --unattended --url "https://github.com/$repository" --name $Name --no-default-labels --labels $Label --work _work --ephemeral --disableupdate
    if ($LASTEXITCODE -ne 0) { throw 'Runner registration failed.' }
} finally {
    Remove-Item Env:ACTIONS_RUNNER_INPUT_TOKEN -ErrorAction SilentlyContinue
    if ($registrationPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($registrationPointer) }
    if ($null -ne $secureRegistration) { $secureRegistration.Dispose() }
    Pop-Location
}
Write-Output 'Registered for one job only. This does not establish isolation or approve arbitrary workflow code. No Windows service was installed.'
if ($Start) {
    $controllerRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    $controllerPython = [IO.Path]::GetFullPath((Join-Path $controllerRoot '..\.venv\Scripts\python.exe'))
    if (-not (Test-Path -LiteralPath $controllerPython -PathType Leaf)) { throw 'The reviewed project Python environment is missing.' }
    $configurationFile = if ($Label -eq 'dsh-cycle-20261028') { '.private\live-cycle.json' } else { '.private\live-smoke.json' }
    if (-not (Test-Path -LiteralPath (Join-Path $controllerRoot $configurationFile) -PathType Leaf)) { throw 'The exact bounded Issue configuration is missing.' }
    $previousSmokeHome = $env:HARNESS_SMOKE_HOME
    $previousSmokePython = $env:HARNESS_SMOKE_PYTHON
    $previousSmokePath = $env:PATH
    $env:HARNESS_SMOKE_HOME = $controllerRoot
    $env:HARNESS_SMOKE_PYTHON = $controllerPython
    $env:PATH = (Split-Path -Parent $controllerPython) + ';' + $env:PATH
    Push-Location $runnerRoot
    try {
        & $runScript
        if ($LASTEXITCODE -ne 0) { throw 'Runner exited with an error; inspect its local diagnostic logs.' }
    } finally {
        Pop-Location
        $env:HARNESS_SMOKE_HOME = $previousSmokeHome
        $env:HARNESS_SMOKE_PYTHON = $previousSmokePython
        $env:PATH = $previousSmokePath
    }
}
