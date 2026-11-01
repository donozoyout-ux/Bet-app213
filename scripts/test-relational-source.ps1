#requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$StartDocker,
    [switch]$InstallDependencies,
    [switch]$PrepareReader,
    [switch]$PromptForSecrets,
    [switch]$LocalOnly,
    [switch]$CheckSource,
    [ValidateRange(5,180)][int]$DockerWaitSeconds = 90
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$savedSettings = @{}

function Set-PrivateSetting([string]$Name, [string]$Label) {
    if (![string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($Name, 'Process'))) { return }
    $savedSettings[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
    $secret = Read-Host $Label -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    try {
        [Environment]::SetEnvironmentVariable($Name, [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer), 'Process')
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $secret.Dispose()
    }
}

function Test-DockerEngine {
    $probe = $null
    try {
        $info = New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName = 'docker.exe'
        $info.Arguments = 'info --format "{{.OSType}}"'
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $probe = [System.Diagnostics.Process]::Start($info)
        if (!$probe.WaitForExit(5000)) { $probe.Kill(); return $false }
        return ($probe.ExitCode -eq 0 -and $probe.StandardOutput.ReadToEnd().Trim() -eq 'linux')
    } catch { return $false }
    finally { if ($null -ne $probe) { $probe.Dispose() } }
}

function Invoke-QuietPython([string]$Executable, [string[]]$CommandArguments) {
    # PS 5.1 may turn harmless native stderr into a terminating ErrorRecord.
    # Discard both streams and use the native exit code instead.
    $ErrorActionPreference = 'Continue'
    & $Executable @CommandArguments *> $null
    return $LASTEXITCODE
}

Push-Location $repoRoot
try {
    if ($LocalOnly -and ($PrepareReader -or $CheckSource)) {
        Write-Output 'BLOCKED conflicting_options'
        exit 1
    }
    if ($StartDocker -and !(Test-DockerEngine)) {
        $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
        if (!(Test-Path -LiteralPath $desktop -PathType Leaf)) {
            Write-Output 'BLOCKED docker_desktop_missing'
            Write-Output 'Bilgi: Docker Desktop kurulumunu tamamlayin; otomatik indirme yapilmadi.'
        } else {
            Start-Process -FilePath $desktop -WindowStyle Hidden | Out-Null
            Write-Output 'Bilgi: Docker Desktop baslatiliyor; Linux motoru bekleniyor.'
            $deadline = [DateTime]::UtcNow.AddSeconds($DockerWaitSeconds)
            while ([DateTime]::UtcNow -lt $deadline) {
                if (Test-DockerEngine) { break }
                Start-Sleep -Seconds 3
            }
        }
    }
    $python = 'python'
    $privatePython = Join-Path $repoRoot 'venv\archive-setup\Scripts\python.exe'
    if ($InstallDependencies) {
        if (!(Test-Path -LiteralPath $privatePython -PathType Leaf)) {
            $nativeCode = Invoke-QuietPython 'python' @('-m', 'venv', (Join-Path $repoRoot 'venv\archive-setup'))
            if ($nativeCode -ne 0) { throw 'bootstrap' }
        }
        $python = $privatePython
        $nativeCode = Invoke-QuietPython $python @('-m', 'pip', '--disable-pip-version-check', '--no-input', 'install', '-r', (Join-Path $repoRoot 'scripts\archive-setup-requirements.txt'))
        if ($nativeCode -ne 0) { throw 'bootstrap' }
    } elseif (Test-Path -LiteralPath $privatePython -PathType Leaf) {
        $python = $privatePython
    }
    $nativeCode = Invoke-QuietPython $python @('-c', 'import src.archive.source_preflight')
    if ($nativeCode -ne 0) {
        Write-Output 'BLOCKED python_dependencies'
        Write-Output 'Bilgi: Python 3.12 kurun ve komutu -InstallDependencies ile yeniden calistirin.'
        exit 1
    }
    if ($PromptForSecrets) {
        Set-PrivateSetting 'RELATIONAL_SOURCE_DATABASE_URL' 'Reader kaynak URL (gizli; bos birakilabilir)'
        Set-PrivateSetting 'RELATIONAL_GITHUB_TOKEN' 'Arsiv GitHub token (gizli; bos birakilabilir)'
        Set-PrivateSetting 'RELATIONAL_ENCRYPTION_KEY' 'Arsiv sifreleme anahtari (gizli; bos birakilabilir)'
        if ($PrepareReader) { Set-PrivateSetting 'RELATIONAL_OWNER_DATABASE_URL' 'Owner URL (yalniz eksik reader olusturma icin; gizli)' }
    }
    if ([string]::IsNullOrWhiteSpace($env:RELATIONAL_GITHUB_REPOSITORY)) {
        $savedSettings['RELATIONAL_GITHUB_REPOSITORY'] = $env:RELATIONAL_GITHUB_REPOSITORY
        $env:RELATIONAL_GITHUB_REPOSITORY = 'donozoyout-ux/kuponanalizii-data-archive'
    }
    $arguments = @('-m', 'src.archive.source_preflight')
    if (!$LocalOnly) { $arguments += @('--check-source', '--check-github') }
    if ($PrepareReader) { $arguments += '--prepare-reader' }
    $ErrorActionPreference = 'Continue'
    $result = & $python @arguments 2>$null
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    $helpText = @{
        source_configuration = 'Reader URL, verify-full ve mevcut CA dosyasini kontrol edin.'
        local_pg18_image = 'Docker Linux motorunu acin; postgres:18 imajini yerel olarak hazirlayin.'
        source_access_and_role_gate = 'Reader hesabi, sifre, dis erisim izni ve salt okunur baglantiyi kontrol edin.'
        select_only_role = 'Reader fazla yetkili veya RLS var; mevcut hesaplari otomatik degistirmiyoruz.'
        reader_preparation = 'Owner URL ve reader URL ayni hedef/CA ile ayarlanmali; mevcut reader degistirilmez.'
        relation_select = 'Reader icin eksik tablo SELECT yetkilerini owner incelemeli.'
        sequence_select = 'Reader icin eksik sequence SELECT yetkilerini owner incelemeli; USAGE/UPDATE vermeyin.'
        schema_usage = 'Reader icin eksik schema USAGE yetkilerini owner incelemeli.'
        docker_tls_connection = 'Docker CA baglama, hostname ve verify-full baglantisini kontrol edin.'
        github_configuration = 'Arsiv token ayarini ve sabit ozel depo adini kontrol edin.'
        github_repository_read = 'Tokenin hedef ozel depoya okuma erisimini kontrol edin.'
        github_releases_read = 'Tokenin arsiv Releases okuma erisimini kontrol edin.'
        github_access = 'GitHub erisimini kontrol edin; ham hata ve token gosterilmedi.'
        encryption_key = 'Gecerli 32-byte base64 arsiv anahtarini guvenli oturuma saglayin.'
    }
    foreach ($line in $result) {
        if ($line -cmatch '^(PASS|BLOCKED|PENDING) ([a-z0-9_]+)$') {
            Write-Output $line
            if ($Matches[1] -eq 'BLOCKED') {
                $check = $Matches[2]
                if ($helpText.ContainsKey($check)) { Write-Output ('Bilgi: ' + $helpText[$check]) }
                else { Write-Output 'Bilgi: Bu kontrol gecmedi; owner kilavuzundaki yetki ve kaynak kosullarini inceleyin.' }
            }
        }
    }
    Write-Output 'Bilgi: Gercek arsiv yuklemesi kapali; bu komut yukleme baslatamaz. Kaynak paritesi henuz kanitlanmadi.'
    if ($code -ne 0) { Write-Output 'BLOCKED preflight'; exit 1 }
    exit 0
} catch {
    Write-Output 'BLOCKED local_runtime'
    Write-Output 'Bilgi: Python/Docker kurulumunu kontrol edin; ham hata ve gizli ayarlar gosterilmedi.'
    exit 1
} finally {
    foreach ($name in $savedSettings.Keys) { [Environment]::SetEnvironmentVariable($name, $savedSettings[$name], 'Process') }
    Pop-Location
}
