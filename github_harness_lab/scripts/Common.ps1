Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-LabDeploymentAuthorization {
    param([string]$AuthorizationFile, [string]$Repository)
    if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw 'An exact owner/repository is required.' }
    if (-not $AuthorizationFile -or -not (Test-Path -LiteralPath $AuthorizationFile -PathType Leaf)) { throw 'Explicit user deployment authorization file is missing.' }
    $authorization = Get-Content -LiteralPath $AuthorizationFile -Raw | ConvertFrom-Json
    if ($authorization.repository -cne $Repository -or 'deploy' -notin $authorization.scopes) { throw 'Repository or deploy scope is not authorized.' }
    if ($authorization.expires_at -notmatch '(?:Z|[+-]\d\d:\d\d)$') { throw 'Authorization expiry must include an explicit UTC offset.' }
    if ([DateTimeOffset]::Parse($authorization.expires_at) -le [DateTimeOffset]::UtcNow) { throw 'Authorization expired.' }
    if ($authorization.isolated_environment_verified -isnot [bool] -or $authorization.isolated_environment_verified -ne $true) { throw 'A verified isolated deployment environment is required.' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $administratorsMember = @($identity.Groups | ForEach-Object { $_.Value }) -contains 'S-1-5-32-544'
    if ($administratorsMember -or $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this script as a dedicated non-administrator account inside the isolated host. A non-elevated administrator token is also refused.' }
}

function Resolve-LabRoot {
    param([string]$Root)
    if (-not [IO.Path]::IsPathRooted($Root)) { throw 'Root must be an absolute path.' }
    $resolved = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($resolved -match '[%"&|<>^!\r\n]' -or $resolved.Length -lt 8) { throw 'Use a dedicated simple absolute path, such as C:\dsh-lab.' }
    $protected = @($env:SystemRoot, $env:USERPROFILE, $env:ProgramFiles, ${env:ProgramFiles(x86)})
    foreach ($entry in $protected) {
        if ($entry -and $resolved -ieq [IO.Path]::GetFullPath($entry).TrimEnd('\')) { throw 'Root cannot be a system or account root.' }
    }
    return $resolved
}
