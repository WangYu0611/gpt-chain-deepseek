[CmdletBinding()]
param([string]$Root='C:\dsh-lab')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$checks = [ordered]@{
    Mode='READ_ONLY'; OS=[Environment]::OSVersion.VersionString; Is64Bit=[Environment]::Is64BitOperatingSystem;
    IsAdministrator=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator);
    AdministratorsGroupMember=(@($identity.Groups | ForEach-Object { $_.Value }) -contains 'S-1-5-32-544');
    RootExists=(Test-Path -LiteralPath $Root); DshOnPath=[bool](Get-Command dsh -ErrorAction SilentlyContinue);
    GitOnPath=[bool](Get-Command git -ErrorAction SilentlyContinue); NvccOnPath=[bool](Get-Command nvcc -ErrorAction SilentlyContinue);
    Note='No model, runner, network test, service or scheduled task is started.'
}
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $checks.Gpu = @(& nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader)
}
if (Get-Command nvcc -ErrorAction SilentlyContinue) { $checks.CudaCompiler = @(& nvcc --version) }
$checks.Disks = @(Get-PSDrive -PSProvider FileSystem | Select-Object Name,Used,Free)
$checks | ConvertTo-Json -Depth 5
