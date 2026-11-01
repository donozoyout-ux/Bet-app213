param(
    [Parameter(Mandatory=$true)][string]$OutputRoot
)
$ErrorActionPreference = 'Stop'
if ($env:RELATIONAL_ARCHIVE_ACTIVATED -ne 'true') { throw 'Owner must explicitly set RELATIONAL_ARCHIVE_ACTIVATED=true' }
foreach ($name in @('RELATIONAL_SOURCE_DATABASE_URL','RELATIONAL_GITHUB_TOKEN','RELATIONAL_GITHUB_REPOSITORY','RELATIONAL_ENCRYPTION_KEY')) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) { throw "Protected setting is missing: $name" }
}
if ($env:RELATIONAL_GITHUB_REPOSITORY -ne 'donozoyout-ux/kuponanalizii-data-archive') { throw 'Unexpected archive repository' }
if (Test-Path -LiteralPath $OutputRoot) { throw 'Output directory must be new' }
$fullOutput = [IO.Path]::GetFullPath($OutputRoot)
$parent = [IO.Path]::GetDirectoryName($fullOutput)
if (!(Test-Path -LiteralPath $parent -PathType Container)) { throw 'Output parent directory missing' }
docker image inspect postgres:18 *> $null
if ($LASTEXITCODE -ne 0) { throw 'Pinned postgres:18 image must already exist locally; this script does not pull images' }
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
$directory = New-Object System.IO.DirectoryInfo($fullOutput)
$directory.Create($acl)
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    python -m src.archive.relational_archive --execute --output-root $fullOutput
    if ($LASTEXITCODE -ne 0) { throw 'Relational archive incomplete; use only the sanitized stage line' }
} finally { Pop-Location }
