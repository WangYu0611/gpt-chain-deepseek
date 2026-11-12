$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$relayUtf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $relayUtf8
$OutputEncoding = $relayUtf8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$relayPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$relayEntry = Join-Path $PSScriptRoot 'bridge.py'
if (-not (Test-Path -LiteralPath $relayPython -PathType Leaf)) {
    Write-Host 'Missing .venv Python. Run setup.ps1 first.' -ForegroundColor Red
    exit 1
}

$relayExitCode = 0
Push-Location -LiteralPath $PSScriptRoot
try {
    & $relayPython -u $relayEntry @args
    $relayExitCode = $LASTEXITCODE
}
catch {
    Write-Host ('Bridge failed: ' + $_.Exception.Message) -ForegroundColor Red
    $relayExitCode = 1
}
finally {
    Pop-Location
}
exit $relayExitCode
