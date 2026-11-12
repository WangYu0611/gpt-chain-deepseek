$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$relayUtf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $relayUtf8
$OutputEncoding = $relayUtf8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

function Test-RelayPython {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Prefix = @()
    )
    try {
        & $Executable @Prefix -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

$relayExitCode = 0
Push-Location -LiteralPath $PSScriptRoot
try {
    $relayVenvDirectory = Join-Path $PSScriptRoot '.venv'
    $relayVenvPython = Join-Path $relayVenvDirectory 'Scripts\python.exe'
    if (Test-Path -LiteralPath $relayVenvDirectory) {
        if (-not (Test-Path -LiteralPath $relayVenvPython -PathType Leaf) -or
            -not (Test-RelayPython -Executable $relayVenvPython)) {
            throw 'Existing .venv is not a valid Python 3.10+ environment. Resolve it before retrying.'
        }
        Write-Host 'Using existing .venv.'
    }
    else {
        $relayCandidates = @()
        $relayLauncher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($relayLauncher) {
            $relayCandidates += @{ Executable = $relayLauncher.Source; Prefix = @('-3') }
        }
        $relaySystemPython = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($relaySystemPython -and $relaySystemPython.Source -notmatch '\\Microsoft\\WindowsApps\\') {
            $relayCandidates += @{ Executable = $relaySystemPython.Source; Prefix = @() }
        }
        $relayCandidates += @{
            Executable = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
            Prefix = @()
        }
        $relayCandidates += @{
            Executable = Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness\resources\runtime\primary-runtime\dependencies\python\python.exe'
            Prefix = @()
        }
        $relaySelectedPython = $null
        foreach ($relayCandidate in $relayCandidates) {
            if ((Test-Path -LiteralPath $relayCandidate.Executable -PathType Leaf) -and
                (Test-RelayPython -Executable $relayCandidate.Executable -Prefix $relayCandidate.Prefix)) {
                $relaySelectedPython = $relayCandidate
                break
            }
        }
        if ($null -eq $relaySelectedPython) {
            throw 'No usable Python 3.10+ with venv/ensurepip found. Install Python and retry.'
        }
        Write-Host ('Creating .venv with: ' + $relaySelectedPython.Executable)
        $relayPythonExecutable = $relaySelectedPython.Executable
        $relayPythonPrefix = $relaySelectedPython.Prefix
        & $relayPythonExecutable @relayPythonPrefix -m venv $relayVenvDirectory
        if ($LASTEXITCODE -ne 0) {
            throw 'Python failed to create .venv.'
        }
    }
    & $relayVenvPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        throw 'Dependency installation failed. Check the pip output and network, then retry setup.ps1.'
    }
    Write-Host 'Setup complete. Next: .\bridge.ps1 doctor'
    Write-Host 'No Chrome download, browser session, or DSH task was started.'
}
catch {
    Write-Host ('Setup failed: ' + $_.Exception.Message) -ForegroundColor Red
    $relayExitCode = 1
}
finally {
    Pop-Location
}
exit $relayExitCode
