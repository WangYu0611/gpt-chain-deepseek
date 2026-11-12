[CmdletBinding()]
param([Parameter(Mandatory=$true)][ValidateRange(0,2)][int]$Index, [switch]$Execute)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$cycleRoot = Join-Path $env:LOCALAPPDATA "HarnessGithubSmoke\cycle-runner-$Index"
$labRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$config = Get-Content -Raw -LiteralPath (Join-Path $labRoot '.private\live-cycle.json') | ConvertFrom-Json
$deadline = if ($config.expires_at -is [DateTime]) { $config.expires_at.ToUniversalTime() } else { [DateTime]::Parse($config.expires_at, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime() }
if (-not $Execute) {
    [pscustomobject]@{Mode='PLAN_ONLY';Root=$cycleRoot;Deadline=$config.expires_at;Service=$false;Hidden=$true} | ConvertTo-Json
    return
}
$remaining = ($deadline - [DateTime]::UtcNow).TotalSeconds
if ($remaining -le 0 -or $remaining -gt 5400 -or -not (Test-Path -LiteralPath (Join-Path $cycleRoot '.runner'))) { throw 'Missing registration or invalid bounded deadline.' }
$env:HARNESS_SMOKE_HOME = $labRoot
$env:HARNESS_SMOKE_PYTHON = [IO.Path]::GetFullPath((Join-Path $labRoot '..\.venv\Scripts\python.exe'))
$env:PATH = (Split-Path -Parent $env:HARNESS_SMOKE_PYTHON) + ';' + $env:PATH
$listener = Start-Process -FilePath (Join-Path $cycleRoot 'run.cmd') -WorkingDirectory $cycleRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $cycleRoot 'listener.stdout.log') -RedirectStandardError (Join-Path $cycleRoot 'listener.stderr.log')
Write-Output "Started one-job Runner $Index with process $($listener.Id); hard deadline $($config.expires_at)."
try {
    while (-not $listener.WaitForExit(10000)) {
        if ([DateTime]::UtcNow -ge $deadline) {
            $listener.Kill($true)
            $listener.WaitForExit()
            Write-Output 'Bounded listener deadline reached; process tree stopped.'
            exit 124
        }
    }
    Write-Output "One-job Runner $Index exited with code $($listener.ExitCode)."
    exit $listener.ExitCode
} finally {
    if (-not $listener.HasExited) { $listener.Kill($true) }
}
