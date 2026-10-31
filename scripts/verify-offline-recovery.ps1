param(
    [Parameter(Mandatory=$true)][string]$Zip,
    [Parameter(Mandatory=$true)][string]$KeyFile,
    [Parameter(Mandatory=$true)][string]$OutputRoot
)
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath $OutputRoot) { throw 'Output directory must be new' }
if (!(Test-Path -LiteralPath $Zip -PathType Leaf)) { throw 'ZIP missing' }
if (!(Test-Path -LiteralPath $KeyFile -PathType Leaf)) { throw 'Key file missing' }
$fullOutput = [IO.Path]::GetFullPath($OutputRoot)
if (!(Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($fullOutput)) -PathType Container)) { throw 'Parent directory missing' }
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
$directory = New-Object System.IO.DirectoryInfo($fullOutput)
# Windows PowerShell 5.1 creates the directory with its restrictive ACL atomically.
$directory.Create($acl)
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    python -m src.archive.offline_recovery --zip $Zip --key-file $KeyFile --output-root $fullOutput
    if ($LASTEXITCODE -ne 0) { throw 'Local recovery incomplete; consult sanitized stage/SQLSTATE' }
} finally { Pop-Location }
