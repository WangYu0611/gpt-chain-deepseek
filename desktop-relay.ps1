$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$windowRelayUtf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $windowRelayUtf8
$OutputEncoding = $windowRelayUtf8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$windowRelayPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$windowRelayEntry = Join-Path $PSScriptRoot 'desktop_relay.py'
$windowRelayExit = 0
Push-Location -LiteralPath $PSScriptRoot
try {
    if (Test-Path -LiteralPath $windowRelayPython -PathType Leaf) {
        & $windowRelayPython -u $windowRelayEntry @args
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -u $windowRelayEntry @args
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -u $windowRelayEntry @args
    } else {
        throw 'Python 3.10+ is required. See README.md for desktop UI dependencies.'
    }
    $windowRelayExit = $LASTEXITCODE
} catch {
    Write-Host ('Window relay failed: ' + $_.Exception.Message) -ForegroundColor Red
    $windowRelayExit = 1
} finally {
    Pop-Location
}
exit $windowRelayExit
