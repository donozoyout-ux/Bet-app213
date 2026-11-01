#requires -Version 5.1
[CmdletBinding()]
param([switch]$CheckSource)
$ErrorActionPreference = 'Stop'
# Never print environment values, native stderr, exceptions or connection strings.
# Without -CheckSource this performs local checks only.
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    $arguments = @('-m', 'src.archive.source_preflight')
    if ($CheckSource) { $arguments += '--check-source' }
    $result = & python @arguments 2>$null
    $code = $LASTEXITCODE
    # Only allow the helper's fixed protocol onto the console.
    foreach ($line in $result) {
        if ($line -cmatch '^(PASS|BLOCKED|PENDING) [a-z_]+$') { Write-Output $line }
    }
    if ($code -ne 0) { Write-Output 'BLOCKED preflight'; exit 1 }
    exit 0
} catch {
    Write-Output 'BLOCKED local_runtime'
    exit 1
} finally { Pop-Location }
