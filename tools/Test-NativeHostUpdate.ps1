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

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$testRoot = Join-Path $root ("data\temp\native-update-test-" + [Guid]::NewGuid().ToString('N'))
$audioCandidate = Join-Path $testRoot 'audio-host-next\stem-studio-audio-host.exe'
$airplayCandidateRoot = Join-Path $testRoot 'airplay-host-next'
$airplayCandidate = Join-Path $airplayCandidateRoot 'bin\stem-studio-airplay-host.exe'
$liveRoot = Join-Path $testRoot 'data\live'
$statusPath = Join-Path $liveRoot 'airplay-status.json'

try {
    New-Item -ItemType Directory -Force -Path `
        (Split-Path -Parent $audioCandidate), `
        (Split-Path -Parent $airplayCandidate), `
        $liveRoot | Out-Null
    [System.IO.File]::WriteAllBytes($audioCandidate, [byte[]](1, 2, 3, 4))
    [System.IO.File]::WriteAllBytes($airplayCandidate, [byte[]](5, 6, 7, 8))
    $pluginPath = Join-Path $airplayCandidateRoot 'lib\gstreamer-1.0\test-plugin.dll'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $pluginPath) | Out-Null
    [System.IO.File]::WriteAllBytes($pluginPath, [byte[]](9, 10, 11, 12))

    $packageCopy = Join-Path $testRoot 'airplay-host-copy'
    Copy-Item -LiteralPath $airplayCandidateRoot -Destination $packageCopy -Recurse
    $packageHash = Get-DirectoryManifestHash -Root $airplayCandidateRoot
    Assert-Equal $packageHash (Get-DirectoryManifestHash -Root $packageCopy) `
        'An intact recursive package copy must preserve the manifest hash'
    [System.IO.File]::WriteAllBytes(
        (Join-Path $packageCopy 'lib\gstreamer-1.0\test-plugin.dll'),
        [byte[]](9, 10, 11, 13)
    )
    Assert-True ($packageHash -ne (Get-DirectoryManifestHash -Root $packageCopy)) `
        'Changing one runtime DLL must change the package manifest hash'

    $now = [DateTime]::SpecifyKind([DateTime]'2026-07-15T08:00:00', [DateTimeKind]::Utc)
    '{"state":"streaming","pcm_frames":100}' | Set-Content -LiteralPath $statusPath -Encoding ascii
    [System.IO.File]::SetLastWriteTimeUtc($statusPath, $now)

    $fresh = Get-NativeUpdateReadiness `
        -Root $testRoot `
        -AudioCandidate $audioCandidate `
        -AirPlayCandidateRoot $airplayCandidateRoot `
        -NowUtc $now `
        -StreamingStatusMaxAgeSeconds 5
    Assert-Equal $false $fresh.Ready 'Fresh AirPlay PCM must block native update'
    Assert-Equal 'ActiveStream' $fresh.Code 'Fresh AirPlay rejection code'

    [System.IO.File]::SetLastWriteTimeUtc($statusPath, $now.AddSeconds(-10))
    $stale = Get-NativeUpdateReadiness `
        -Root $testRoot `
        -AudioCandidate $audioCandidate `
        -AirPlayCandidateRoot $airplayCandidateRoot `
        -NowUtc $now `
        -StreamingStatusMaxAgeSeconds 5
    Assert-Equal $true $stale.Ready 'Stale AirPlay PCM must allow a controlled update'
    Assert-Equal 'Ready' $stale.Code 'Stale AirPlay readiness code'

    $expectedAudio = (Get-FileHash -LiteralPath $audioCandidate -Algorithm SHA256).Hash
    $expectedAirPlay = (Get-FileHash -LiteralPath $airplayCandidate -Algorithm SHA256).Hash
    $matching = Get-NativeUpdateReadiness `
        -Root $testRoot `
        -AudioCandidate $audioCandidate `
        -AirPlayCandidateRoot $airplayCandidateRoot `
        -ExpectedAudioHash $expectedAudio `
        -ExpectedAirPlayHash $expectedAirPlay `
        -NowUtc $now
    Assert-Equal $true $matching.Ready 'Matching candidate hashes must pass'
    Assert-Equal $expectedAudio.ToUpperInvariant() $matching.AudioHash 'Audio hash is recorded'
    Assert-Equal $expectedAirPlay.ToUpperInvariant() $matching.AirPlayHash 'AirPlay hash is recorded'

    $mismatch = Get-NativeUpdateReadiness `
        -Root $testRoot `
        -AudioCandidate $audioCandidate `
        -AirPlayCandidateRoot $airplayCandidateRoot `
        -ExpectedAudioHash ('0' * 64) `
        -NowUtc $now
    Assert-Equal $false $mismatch.Ready 'A candidate hash mismatch must block update'
    Assert-Equal 'AudioHashMismatch' $mismatch.Code 'Hash mismatch rejection code'

    Assert-Equal $true (Test-AirPlayPcmQuiet -BeforePcmFrames 100 -AfterPcmFrames 100) `
        'Unchanged PCM counter is quiet'
    Assert-Equal $false (Test-AirPlayPcmQuiet -BeforePcmFrames 100 -AfterPcmFrames 101) `
        'Advancing PCM counter is active'

    $existingSequence = 1800000000000000000L
    $nextSequence = Get-NativeCommandSequence -CurrentSequence $existingSequence -NowUtc $now
    Assert-True ($nextSequence -gt $existingSequence) `
        'The post-restart command sequence must exceed the existing Python time_ns value'
    Assert-Equal 1800000000000000001L $nextSequence `
        'Existing larger command sequence advances by one'

    $clockSequence = Get-NativeCommandSequence -CurrentSequence 1 -NowUtc $now
    Assert-Equal 1784102400000000000L $clockSequence `
        'UTC time produces a Python-compatible nanosecond command sequence'

    $layout = Get-NativeHostInstallLayout -Root $testRoot -TransactionId 'unit-test'
    foreach ($path in @(
        $layout.AudioLive,
        $layout.AirPlayLive,
        $layout.StageRoot,
        $layout.BackupRoot,
        $layout.StatePath
    )) {
        Assert-True (Test-WorkspaceContainedPath -Root $testRoot -Path $path) `
            "Install path escaped the workspace: $path"
    }

    $escaped = Join-Path (Split-Path -Parent $testRoot) 'outside\candidate.exe'
    Assert-Equal $false (Test-WorkspaceContainedPath -Root $testRoot -Path $escaped) `
        'Sibling paths must not pass workspace containment'

    Remove-Item -LiteralPath $statusPath -Force
    $missingStatus = Get-NativeUpdateReadiness `
        -Root $testRoot `
        -AudioCandidate $audioCandidate `
        -AirPlayCandidateRoot $airplayCandidateRoot `
        -NowUtc $now
    Assert-Equal $true $missingStatus.Ready 'Missing AirPlay status is safe after process checks'

    'Native host update core tests: PASS'
} finally {
    $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
    if ((Test-Path -LiteralPath $resolvedTestRoot) -and
        (Test-WorkspaceContainedPath -Root $root -Path $resolvedTestRoot)) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
