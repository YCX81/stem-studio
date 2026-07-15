$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot '..\scripts\NativeHostUpdate.Core.ps1')

function Assert-Equal($Expected, $Actual, [string]$Message) {
    if ($Expected -ne $Actual) {
        throw "$Message (expected=$Expected, actual=$Actual)"
    }
}

function Assert-True([bool]$Value, [string]$Message) {
    if (-not $Value) { throw $Message }
}

$workspace = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$testRoot = Join-Path $workspace ("data\temp\native-installer-test-" + [Guid]::NewGuid().ToString('N'))
$rollbackRoot = Join-Path $workspace ("data\temp\native-rollback-test-" + [Guid]::NewGuid().ToString('N'))
$installer = Join-Path $workspace 'scripts\Install-NativeHosts.ps1'
$audioCandidate = Join-Path $testRoot 'candidates\audio\stem-studio-audio-host.exe'
$airplayCandidateRoot = Join-Path $testRoot 'candidates\airplay-host'
$airplayCandidate = Join-Path $airplayCandidateRoot 'bin\stem-studio-airplay-host.exe'
$airplayPlugin = Join-Path $airplayCandidateRoot 'lib\gstreamer-1.0\plugin.dll'
$liveAudio = Join-Path $testRoot 'host\bin\stem-studio-audio-host.exe'
$liveAirPlayExe = Join-Path $testRoot 'airplay-host\bin\stem-studio-airplay-host.exe'
$statusPath = Join-Path $testRoot 'data\live\airplay-status.json'
$rollbackProcesses = @()

try {
    foreach ($path in @($audioCandidate, $airplayCandidate, $airplayPlugin, $liveAudio, $liveAirPlayExe, $statusPath)) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
    }
    [System.IO.File]::WriteAllBytes($audioCandidate, [byte[]](1, 2, 3, 4))
    [System.IO.File]::WriteAllBytes($airplayCandidate, [byte[]](5, 6, 7, 8))
    [System.IO.File]::WriteAllBytes($airplayPlugin, [byte[]](9, 10, 11, 12))
    [System.IO.File]::WriteAllBytes($liveAudio, [byte[]](20, 21, 22, 23))
    [System.IO.File]::WriteAllBytes($liveAirPlayExe, [byte[]](24, 25, 26, 27))
    '{"state":"streaming","pcm_frames":123}' | Set-Content -LiteralPath $statusPath -Encoding ascii

    $readiness = (& $installer `
        -Root $testRoot `
        -AudioCandidate $audioCandidate `
        -AirPlayCandidateRoot $airplayCandidateRoot `
        -ReadinessOnly | Out-String) | ConvertFrom-Json
    Assert-Equal $false $readiness.Ready 'Fresh active PCM must be rejected by read-only preflight'
    Assert-Equal 'ActiveStream' $readiness.Code 'Active preflight code'
    Assert-Equal $false (Test-Path -LiteralPath (Join-Path $testRoot 'data\temp\native-update-state.json')) `
        'Read-only preflight must not create update state'

    Remove-Item -LiteralPath $statusPath -Force
    $oldAudioHash = (Get-FileHash -LiteralPath $liveAudio -Algorithm SHA256).Hash
    $oldAirPlayHash = (Get-FileHash -LiteralPath $liveAirPlayExe -Algorithm SHA256).Hash
    $prepared = (& $installer `
        -Root $testRoot `
        -AudioCandidate $audioCandidate `
        -AirPlayCandidateRoot $airplayCandidateRoot `
        -QuietProbeSeconds 1 `
        -PrepareOnly | Out-String) | ConvertFrom-Json

    Assert-Equal 'prepared' $prepared.state 'Prepare-only state'
    Assert-Equal $false $prepared.docker_restarted 'Native preparation must not restart Docker'
    Assert-Equal $oldAudioHash (Get-FileHash -LiteralPath $liveAudio -Algorithm SHA256).Hash `
        'Prepare-only must not replace the installed audio host'
    Assert-Equal $oldAirPlayHash (Get-FileHash -LiteralPath $liveAirPlayExe -Algorithm SHA256).Hash `
        'Prepare-only must not replace the installed AirPlay host'
    Assert-Equal $false (Test-Path -LiteralPath (Join-Path $testRoot 'data\live\controller.pid')) `
        'Prepare-only must not launch a controller'

    $transactionRoot = [string]$prepared.transaction_root
    Assert-True (Test-WorkspaceContainedPath -Root $testRoot -Path $transactionRoot) `
        'Prepared transaction must stay inside the update root'
    Assert-True (Test-Path -LiteralPath (Join-Path $transactionRoot 'stage\audio-host\stem-studio-audio-host.exe')) `
        'Prepared transaction must contain the staged audio host'
    Assert-True (Test-Path -LiteralPath (Join-Path $transactionRoot 'stage\airplay-host\lib\gstreamer-1.0\plugin.dll')) `
        'Prepared transaction must contain the complete AirPlay runtime package'

    $candidateAudioHash = (Get-FileHash -LiteralPath $audioCandidate -Algorithm SHA256).Hash
    $candidateAirPlayHash = (Get-FileHash -LiteralPath $airplayCandidate -Algorithm SHA256).Hash
    $candidatePackageHash = Get-DirectoryManifestHash -Root $airplayCandidateRoot
    $installed = (& $installer `
        -Root $testRoot `
        -AudioCandidate $audioCandidate `
        -AirPlayCandidateRoot $airplayCandidateRoot `
        -ExpectedAudioHash $candidateAudioHash `
        -ExpectedAirPlayHash $candidateAirPlayHash `
        -QuietProbeSeconds 1 `
        -NoResume | Out-String) | ConvertFrom-Json

    Assert-Equal 'installed' $installed.state 'Isolated atomic swap state'
    Assert-Equal $false $installed.docker_restarted 'Atomic native swap must not restart Docker'
    Assert-Equal $false $installed.controller_restarted 'No inactive test controller should be launched'
    Assert-Equal $candidateAudioHash (Get-FileHash -LiteralPath $liveAudio -Algorithm SHA256).Hash `
        'Atomic swap must install the verified audio host'
    Assert-Equal $candidateAirPlayHash (Get-FileHash -LiteralPath $liveAirPlayExe -Algorithm SHA256).Hash `
        'Atomic swap must install the verified AirPlay executable'
    Assert-Equal $candidatePackageHash (Get-DirectoryManifestHash -Root (Join-Path $testRoot 'airplay-host')) `
        'Atomic swap must install the complete verified AirPlay package'
    $installedTransaction = [string]$installed.transaction_root
    Assert-True (Test-Path -LiteralPath (Join-Path $installedTransaction 'backup\audio-host\stem-studio-audio-host.exe')) `
        'Atomic swap must retain the previous audio host for rollback'
    Assert-True (Test-Path -LiteralPath (Join-Path $installedTransaction 'backup\airplay-host\bin\stem-studio-airplay-host.exe')) `
        'Atomic swap must retain the previous AirPlay package for rollback'

    $rollbackAudioCandidate = Join-Path $rollbackRoot 'candidates\audio\stem-studio-audio-host.exe'
    $rollbackAirPlayCandidateRoot = Join-Path $rollbackRoot 'candidates\airplay-host'
    $rollbackAirPlayCandidate = Join-Path $rollbackAirPlayCandidateRoot 'bin\stem-studio-airplay-host.exe'
    $rollbackCandidatePlugin = Join-Path $rollbackAirPlayCandidateRoot 'lib\gstreamer-1.0\plugin.dll'
    $rollbackLiveAudio = Join-Path $rollbackRoot 'host\bin\stem-studio-audio-host.exe'
    $rollbackLiveAirPlay = Join-Path $rollbackRoot 'airplay-host\bin\stem-studio-airplay-host.exe'
    $rollbackLivePlugin = Join-Path $rollbackRoot 'airplay-host\lib\gstreamer-1.0\old-plugin.dll'
    $rollbackLive = Join-Path $rollbackRoot 'data\live'
    foreach ($path in @(
        $rollbackAudioCandidate,
        $rollbackAirPlayCandidate,
        $rollbackCandidatePlugin,
        $rollbackLiveAudio,
        $rollbackLiveAirPlay,
        $rollbackLivePlugin,
        (Join-Path $rollbackLive 'controller-status.json')
    )) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
    }
    # The candidate files pass hashing but intentionally are not Windows executables.
    [System.IO.File]::WriteAllBytes($rollbackAudioCandidate, [byte[]](31, 32, 33, 34))
    [System.IO.File]::WriteAllBytes($rollbackAirPlayCandidate, [byte[]](35, 36, 37, 38))
    [System.IO.File]::WriteAllBytes($rollbackCandidatePlugin, [byte[]](39, 40, 41, 42))
    Copy-Item -LiteralPath "$env:SystemRoot\System32\ping.exe" -Destination $rollbackLiveAudio
    Copy-Item -LiteralPath "$env:SystemRoot\System32\ping.exe" -Destination $rollbackLiveAirPlay
    [System.IO.File]::WriteAllBytes($rollbackLivePlugin, [byte[]](43, 44, 45, 46))

    $oldRollbackAudioHash = (Get-FileHash -LiteralPath $rollbackLiveAudio -Algorithm SHA256).Hash
    $oldRollbackAirPlayHash = (Get-FileHash -LiteralPath $rollbackLiveAirPlay -Algorithm SHA256).Hash
    $oldRollbackPackageHash = Get-DirectoryManifestHash -Root (Join-Path $rollbackRoot 'airplay-host')
    $rollbackPlayback = Start-Process -FilePath $rollbackLiveAudio `
        -ArgumentList @('127.0.0.1', '-t') -WindowStyle Hidden -PassThru
    $rollbackAirPlay = Start-Process -FilePath $rollbackLiveAirPlay `
        -ArgumentList @('127.0.0.1', '-t') -WindowStyle Hidden -PassThru
    $rollbackController = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList '-NoProfile -Command "Start-Sleep -Seconds 60"' `
        -WindowStyle Hidden -PassThru
    $rollbackProcesses = @($rollbackPlayback, $rollbackAirPlay, $rollbackController)
    Assert-True (-not $rollbackPlayback.HasExited) 'Rollback fixture playback process must be alive'
    Assert-True (-not $rollbackAirPlay.HasExited) 'Rollback fixture AirPlay process must be alive'
    Assert-True (-not $rollbackController.HasExited) 'Rollback fixture controller process must be alive'

    [ordered]@{
        state = 'airplay_waiting'
        input_source = 'airplay'
        profile_name = 'Six-track realtime'
        track_count = 6
        host_pid = $rollbackPlayback.Id
        airplay_pid = $rollbackAirPlay.Id
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $rollbackLive 'controller-status.json') -Encoding UTF8
    [ordered]@{
        sequence = 1800000000000000000L
        action = 'start_airplay'
        monitor_stem = 'mix'
        profile_name = 'Six-track realtime'
        track_count = 6
        input_source = 'airplay'
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $rollbackLive 'command.json') -Encoding UTF8
    $rollbackController.Id | Set-Content -LiteralPath (Join-Path $rollbackLive 'controller.pid') -Encoding ascii

    $rollbackFailure = ''
    $rollbackOutput = ''
    try {
        $rollbackOutput = (& $installer `
            -Root $rollbackRoot `
            -AudioCandidate $rollbackAudioCandidate `
            -AirPlayCandidateRoot $rollbackAirPlayCandidateRoot `
            -QuietProbeSeconds 1 `
            -NativeStopTimeoutSeconds 1 `
            -ControllerTimeoutSeconds 5 | Out-String)
    } catch {
        $rollbackFailure = $_.Exception.Message
    }
    Assert-True ($rollbackFailure -like '*rolled back*') `
        "An invalid installed candidate must fail and report automatic rollback. failure=$rollbackFailure output=$rollbackOutput"
    $rollbackStatePath = Join-Path $rollbackRoot 'data\temp\native-update-state.json'
    $rollbackState = Get-Content -LiteralPath $rollbackStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Equal 'rolled_back' $rollbackState.state 'Injected startup failure rollback state'
    Assert-Equal $false $rollbackState.docker_restarted 'Rollback path must not restart Docker'
    Assert-Equal $oldRollbackAudioHash (Get-FileHash -LiteralPath $rollbackLiveAudio -Algorithm SHA256).Hash `
        'Rollback must restore the previous audio host'
    Assert-Equal $oldRollbackAirPlayHash (Get-FileHash -LiteralPath $rollbackLiveAirPlay -Algorithm SHA256).Hash `
        'Rollback must restore the previous AirPlay executable'
    Assert-Equal $oldRollbackPackageHash (Get-DirectoryManifestHash -Root (Join-Path $rollbackRoot 'airplay-host')) `
        'Rollback must restore the complete previous AirPlay package'

    'Native host installer prepare, atomic swap, and rollback tests: PASS'
} finally {
    if (Test-Path -LiteralPath (Join-Path $rollbackRoot 'data\live\controller-status.json')) {
        try {
            $cleanupStatus = Get-Content -LiteralPath (Join-Path $rollbackRoot 'data\live\controller-status.json') `
                -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($cleanupId in @($cleanupStatus.host_pid, $cleanupStatus.airplay_pid)) {
                if ($cleanupId) { Stop-Process -Id ([int]$cleanupId) -Force -ErrorAction SilentlyContinue }
            }
        } catch { }
    }
    if (Test-Path -LiteralPath (Join-Path $rollbackRoot 'data\live\controller.pid')) {
        try {
            Stop-Process -Id ([int](Get-Content -LiteralPath (Join-Path $rollbackRoot 'data\live\controller.pid') -Raw)) `
                -Force -ErrorAction SilentlyContinue
        } catch { }
    }
    foreach ($process in $rollbackProcesses) {
        if ($null -ne $process) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    }
    $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
    if ((Test-Path -LiteralPath $resolvedTestRoot) -and
        (Test-WorkspaceContainedPath -Root $workspace -Path $resolvedTestRoot)) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
    $resolvedRollbackRoot = [System.IO.Path]::GetFullPath($rollbackRoot)
    if ((Test-Path -LiteralPath $resolvedRollbackRoot) -and
        (Test-WorkspaceContainedPath -Root $workspace -Path $resolvedRollbackRoot)) {
        Remove-Item -LiteralPath $resolvedRollbackRoot -Recurse -Force
    }
}
