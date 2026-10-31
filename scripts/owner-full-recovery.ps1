[CmdletBinding()]
param(
    [ValidateSet('Preflight','PreflightLocal','InitKey','Backup','LocalBackup')][string]$Mode = 'Preflight',
    [string]$KeyFile,
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,63}$')][string]$BackupId,
    [switch]$KeyRetainedOffline
)
# Owner-operated only. Never pass credentials as parameters, print them, or use Start-Transcript.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$originalLocation = Get-Location
$ownedContainer = $null
$savedEnvironment = @{}
$stage = 'startup'
$environmentNames = @('ARCHIVE_DATABASE_URL','ARCHIVE_ENCRYPTION_KEY','ARCHIVE_GITHUB_TOKEN',
    'RECOVERY_VERIFY_URL','RECOVERY_VERIFY_CONTAINER','RECOVERY_BACKUP_ID','RECOVERY_AUTHORIZED',
    'DATABASE_URL','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID','NVIDIA_API_KEY','NVIDIA_AI_ENABLED',
    'SCRAPER_WORKER_ENABLED','AUTO_BACKFILL_ON_EMPTY','RECOVERY_OFFLINE_DIR','TMP','TEMP')

function Protect-OwnerDirectory([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    if ($resolved.StartsWith([IO.Path]::GetFullPath($repoRoot) + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase) -or $resolved -eq [IO.Path]::GetFullPath($repoRoot)) {
        throw 'Use a private local directory outside this Git checkout.'
    }
    [IO.Directory]::CreateDirectory($resolved) | Out-Null
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl',
        'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
    $directory = [IO.DirectoryInfo]::new($resolved)
    if ($directory.PSObject.Methods['SetAccessControl']) { $directory.SetAccessControl($acl) }
    else { [IO.FileSystemAclExtensions]::SetAccessControl($directory, $acl) }
    return $resolved
}

function Invoke-QuietNative([string]$Program, [string[]]$Arguments) {
    # Windows PowerShell 5.1 treats native stderr as ErrorRecords even with 2>$null.
    # Keep it captured/suppressed and inspect exit status instead of logging it.
    $ErrorActionPreference = 'Continue'
    $output = & $Program @Arguments 2>$null
    return @{ Output=$output; Code=$LASTEXITCODE }
}

function Read-Key([string]$Path) {
    try {
        $encoded = [IO.File]::ReadAllText($Path).Trim()
        $bytes = [Convert]::FromBase64String($encoded.Replace('-','+').Replace('_','/'))
        if ($bytes.Length -ne 32) { throw 'invalid' }
        return $encoded
    } catch { throw 'Key file must decode to exactly 32 bytes. No key value was printed.' }
}

try {
    Set-Location $repoRoot
    if ($env:OS -ne 'Windows_NT') { throw 'Run this owner launcher on Windows PowerShell.' }
    if ($Mode -eq 'InitKey') {
        if (-not $KeyFile) { throw 'Choose a new key file outside the repository.' }
        $keyPath = [IO.Path]::GetFullPath($KeyFile)
        # Only a dedicated private directory is accepted: do not change ACLs on a user folder.
        $parent = Split-Path $keyPath -Parent
        if (Test-Path -LiteralPath $parent) { throw 'Choose a new dedicated directory for this key.' }
        Protect-OwnerDirectory $parent | Out-Null
        $keyBytes = New-Object byte[] 32
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($keyBytes) } finally { $rng.Dispose() }
        $stream = [IO.File]::Open($keyPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $raw = [Text.Encoding]::ASCII.GetBytes([Convert]::ToBase64String($keyBytes))
            $stream.Write($raw, 0, $raw.Length)
        } finally { $stream.Dispose(); [Array]::Clear($keyBytes,0,$keyBytes.Length) }
        $null = Read-Key $keyPath
        Write-Output 'New private key file created and validated. Keep an independent offline copy before Backup.'
        exit 0
    }

    $stage = 'tools_preflight'
    $checks = [ordered]@{}
    foreach ($tool in @('python','docker','gh')) { $checks[$tool] = [bool](Get-Command $tool -ErrorAction SilentlyContinue) }
    if ($checks.docker) {
        $result = Invoke-QuietNative docker @('info','--format','{{.OSType}}')
        $checks.dockerLinuxReady = ($result.Code -eq 0 -and $result.Output -eq 'linux')
    }
    if ($checks.python) {
        $result = Invoke-QuietNative python @('-c','import psycopg, cryptography, httpx; assert psycopg.pq.version() >= 170000')
        $checks.pythonLibraries = ($result.Code -eq 0)
    }
    if ($checks.gh -and ($Mode -eq 'Preflight' -or $Mode -eq 'Backup')) {
        $result = Invoke-QuietNative gh @('auth','token')
        $token = $result.Output
        $checks.githubAuthenticated = ($result.Code -eq 0 -and -not [string]::IsNullOrWhiteSpace($token))
    } else {
        $checks.githubAuthenticated = $false  # Not needed for the local browser-upload path.
    }
    $localDrive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot([Environment]::GetFolderPath('LocalApplicationData')))
    $checks.localWorkspaceSpace = ($localDrive.AvailableFreeSpace -ge 8GB)
    if ($KeyFile) { $null = Read-Key $KeyFile; $checks.keyFileValid = $true }
    foreach ($check in $checks.GetEnumerator()) { Write-Output ('PREFLIGHT {0}: {1}' -f $check.Key, $check.Value) }
    # Local browser-upload path does not use GitHub CLI at all.
    $required = @('python','docker','dockerLinuxReady','pythonLibraries','localWorkspaceSpace')
    if ($Mode -eq 'Preflight' -or $Mode -eq 'Backup') {
        $required += @('gh','githubAuthenticated')
    }
    $missing = @($required | Where-Object { -not $checks.Contains($_) -or -not $checks[$_] })
    if ($Mode -eq 'Preflight' -or $Mode -eq 'PreflightLocal') {
        Write-Output ('PREFLIGHT requiredReady: {0}' -f ($missing.Count -eq 0))
        exit $(if ($missing.Count -ne 0) { 1 } else { 0 })
    }
    if ($missing.Count -gt 0) {
        throw 'Required tools not ready. Run -Mode PreflightLocal for browser-upload recovery.'
    }
    if (-not $KeyFile -or -not $BackupId -or -not $KeyRetainedOffline) {
        throw 'Backup needs KeyFile, a unique BackupId, and owner confirmation KeyRetainedOffline.'
    }
    $stage = 'encryption_key_validation'
    $key = Read-Key $KeyFile
    foreach ($name in $environmentNames) { $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
    $env:ARCHIVE_ENCRYPTION_KEY = $key
    $env:ARCHIVE_GITHUB_TOKEN = if ($Mode -eq 'Backup') { $token } else { '' }
    # Existing credentials are entered only at the local terminal, never in chat, arguments or files.
    $stage = 'source_url_entry'
    $secureSource = Read-Host 'Active Render external PostgreSQL URL (hidden; not saved)' -AsSecureString
    $env:ARCHIVE_DATABASE_URL = [Net.NetworkCredential]::new('', $secureSource).Password
    $stage = 'source_identity_validation'
    $null = & python -m src.archive.owner_recovery check-source 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Active source identity check failed before any connection.' }
    $stage = 'source_readonly_tls_connectivity'
    $null = & python -m src.archive.owner_recovery probe-source-readonly 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Read-only source connection probe failed. Do not start backup.' }
    if ($Mode -eq 'Backup') {
        $stage = 'github_archive_permission_check'
        $null = & python -m src.archive.owner_recovery check-archive 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'Private archive repository access check failed before source export.' }
    }
    $stage = 'local_private_workspace'
    $runRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('KuponRecovery\run-' + $BackupId)
    if (Test-Path -LiteralPath $runRoot) { throw 'Run directory exists. Use a new BackupId; do not overwrite earlier evidence.' }
    $runRoot = Protect-OwnerDirectory $runRoot
    $env:RECOVERY_OFFLINE_DIR = if ($Mode -eq 'LocalBackup') { Join-Path $runRoot 'encrypted-assets' } else { '' }
    $env:TMP = $runRoot
    $env:TEMP = $runRoot
    $env:DATABASE_URL = ''
    $env:TELEGRAM_BOT_TOKEN = ''
    $env:TELEGRAM_CHAT_ID = ''
    $env:NVIDIA_API_KEY = ''
    $env:NVIDIA_AI_ENABLED = 'false'
    $env:SCRAPER_WORKER_ENABLED = 'false'
    $env:AUTO_BACKFILL_ON_EMPTY = 'false'
    $env:RECOVERY_BACKUP_ID = $BackupId
    $env:RECOVERY_AUTHORIZED = 'true'
    $passwordBytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($passwordBytes) } finally { $rng.Dispose() }
    $verifyPassword = [Convert]::ToBase64String($passwordBytes).Replace('+','-').Replace('/','_')
    # On Windows PowerShell 5.1 native stderr can become terminating
    # ErrorRecords even when redirected. Use the existing safe native helper.
    # Pull PG18 explicitly first, before attempting an isolated verifier.
    $stage = 'isolated_postgres_image'
    $imageCheck = Invoke-QuietNative docker @('image','inspect','postgres:18','--format','{{.Id}}')
    if ($imageCheck.Code -ne 0) {
        $pull = Invoke-QuietNative docker @('pull','postgres:18')
        if ($pull.Code -ne 0) { throw 'Cannot fetch postgres:18 image. No containers were removed.' }
    }
    # Avoid a known port conflict rather than deleting or stopping other containers.
    $stage = 'isolated_postgres_port'
    $listening = @(Get-NetTCPConnection -LocalPort 5433 -State Listen -ErrorAction SilentlyContinue)
    if ($listening.Count -gt 0) { throw 'Local verifier port 5433 is occupied. No existing process was changed.' }
    $previousPassword = $env:POSTGRES_PASSWORD
    $env:POSTGRES_PASSWORD = $verifyPassword
    try {
        $stage = 'isolated_postgres_container_start'
        $started = Invoke-QuietNative docker @('run','-d','--rm',
            '--label',"kupon.recovery.owner-run=$BackupId",
            '-p','127.0.0.1:5433:5432','--env','POSTGRES_PASSWORD','postgres:18')
        $containerOutput = @($started.Output | Where-Object { $_ -match '^[0-9a-f]{64}
    $stage = 'isolated_postgres_container_ready'
    $ready = $false
    for ($attempt=0; $attempt -lt 30; $attempt++) {
        $null = & docker exec $ownedContainer pg_isready -U postgres 2>$null
        if ($LASTEXITCODE -eq 0) { $ready=$true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw 'Isolated verifier did not become ready.' }
    $env:RECOVERY_VERIFY_CONTAINER = $ownedContainer
    $env:RECOVERY_VERIFY_URL = 'postgresql://postgres:' + $verifyPassword + '@127.0.0.1:5433/postgres'
    $stage = 'readonly_export_local_restore'
    Push-Location $repoRoot
    try {
        & python -m src.archive.full_recovery | Tee-Object -FilePath (Join-Path $runRoot 'verification.log')
        if ($LASTEXITCODE -ne 0) { throw 'Recovery incomplete. Keep any draft archive; do not clean the source.' }
    } finally { Pop-Location }
} catch {
    # Never print arbitrary native exception text, which may contain credentials.
    # $stage is selected exclusively from constant strings above. Never echo exception
    # text: PowerShell native command errors can contain tokens, DSNs, and paths.
    Write-Output ('OWNER_RECOVERY_INCOMPLETE stage={0}; source unchanged; no cleanup authorized.' -f $stage)
    exit 1
} finally {
    if ($ownedContainer -and $ownedContainer -match '^[0-9a-f]{64}$') {
        $labelJson = & docker inspect --format '{{json .Config.Labels}}' $ownedContainer 2>$null
        $inspected = $LASTEXITCODE -eq 0
        $labels = if ($inspected) { $labelJson | ConvertFrom-Json } else { $null }
        if ($inspected -and $labels.'kupon.recovery.owner-run' -eq $BackupId) {
            $null = & docker rm -f -v $ownedContainer 2>$null
        }
    }
    foreach ($entry in $savedEnvironment.GetEnumerator()) { [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process') }
    $token=$null; $key=$null; $verifyPassword=$null; $secureSource=$null; $result=$null
    Set-Location $originalLocation
}
 }) |
            Select-Object -Last 1
        if ($started.Code -ne 0 -or -not $containerOutput) {
            throw 'Cannot create isolated verifier. No existing container will be removed.'
        }
        $ownedContainer = [string]$containerOutput
    } finally { $env:POSTGRES_PASSWORD = $previousPassword }
    $stage = 'isolated_postgres_container_ready'
    $ready = $false
    for ($attempt=0; $attempt -lt 30; $attempt++) {
        $null = & docker exec $ownedContainer pg_isready -U postgres 2>$null
        if ($LASTEXITCODE -eq 0) { $ready=$true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw 'Isolated verifier did not become ready.' }
    $env:RECOVERY_VERIFY_CONTAINER = $ownedContainer
    $env:RECOVERY_VERIFY_URL = 'postgresql://postgres:' + $verifyPassword + '@127.0.0.1:5433/postgres'
    $stage = 'readonly_export_local_restore'
    Push-Location $repoRoot
    try {
        & python -m src.archive.full_recovery | Tee-Object -FilePath (Join-Path $runRoot 'verification.log')
        if ($LASTEXITCODE -ne 0) { throw 'Recovery incomplete. Keep any draft archive; do not clean the source.' }
    } finally { Pop-Location }
} catch {
    # Never print arbitrary native exception text, which may contain credentials.
    # $stage is selected exclusively from constant strings above. Never echo exception
    # text: PowerShell native command errors can contain tokens, DSNs, and paths.
    Write-Output ('OWNER_RECOVERY_INCOMPLETE stage={0}; source unchanged; no cleanup authorized.' -f $stage)
    exit 1
} finally {
    if ($ownedContainer -and $ownedContainer -match '^[0-9a-f]{64}$') {
        $labelJson = & docker inspect --format '{{json .Config.Labels}}' $ownedContainer 2>$null
        $inspected = $LASTEXITCODE -eq 0
        $labels = if ($inspected) { $labelJson | ConvertFrom-Json } else { $null }
        if ($inspected -and $labels.'kupon.recovery.owner-run' -eq $BackupId) {
            $null = & docker rm -f -v $ownedContainer 2>$null
        }
    }
    foreach ($entry in $savedEnvironment.GetEnumerator()) { [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process') }
    $token=$null; $key=$null; $verifyPassword=$null; $secureSource=$null; $result=$null
    Set-Location $originalLocation
}
