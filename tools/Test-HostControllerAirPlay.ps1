$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot '..\scripts\HostController.Core.ps1')

function Assert-Equal($Expected, $Actual, [string]$Message) {
    if ($Expected -ne $Actual) {
        throw "$Message (expected=$Expected, actual=$Actual)"
    }
}

$connected = Get-AirPlayStartPlan `
    -AirPlayRunning $true `
    -PlaybackRunning $true `
    -CurrentProfile 'profile-two-track' `
    -RequestedProfile 'profile-two-track' `
    -CurrentDeviceEndpoint '192.168.31.88:4010' `
    -RequestedDeviceEndpoint '192.168.31.88:4010'
Assert-Equal $false $connected.RestartAirPlay 'Connected AirPlay must not restart'
Assert-Equal $false $connected.RestartPlayback 'Unchanged profile must not restart playback'

$changedProfile = Get-AirPlayStartPlan `
    -AirPlayRunning $true `
    -PlaybackRunning $true `
    -CurrentProfile 'profile-two-track' `
    -RequestedProfile 'profile-four-track'
Assert-Equal $false $changedProfile.RestartAirPlay 'Changing profile must preserve AirPlay'
Assert-Equal $true $changedProfile.RestartPlayback 'Changing profile must restart playback only'

$changedDevice = Get-AirPlayStartPlan `
    -AirPlayRunning $true `
    -PlaybackRunning $true `
    -CurrentProfile 'profile-two-track' `
    -RequestedProfile 'profile-two-track' `
    -CurrentDeviceEndpoint '' `
    -RequestedDeviceEndpoint '192.168.31.88:4010'
Assert-Equal $false $changedDevice.RestartAirPlay 'Changing device endpoint must preserve AirPlay'
Assert-Equal $true $changedDevice.RestartPlayback 'Changing device endpoint must restart playback only'

$changedHop = Get-AirPlayStartPlan `
    -AirPlayRunning $true `
    -PlaybackRunning $true `
    -CurrentProfile 'profile-two-track' `
    -RequestedProfile 'profile-two-track' `
    -CurrentHopSeconds 6 `
    -RequestedHopSeconds 3
Assert-Equal $false $changedHop.RestartAirPlay 'Changing hop must preserve AirPlay'
Assert-Equal $true $changedHop.RestartPlayback 'Changing hop must restart playback only'

$coldStart = Get-AirPlayStartPlan `
    -AirPlayRunning $false `
    -PlaybackRunning $false `
    -CurrentProfile '' `
    -RequestedProfile 'profile-six-track'
Assert-Equal $true $coldStart.RestartAirPlay 'Cold start must launch AirPlay'
Assert-Equal $true $coldStart.RestartPlayback 'Cold start must launch playback'

Assert-Equal 2 (Assert-LiveTrackCount 2) 'Two-track validation'
Assert-Equal 4 (Assert-LiveTrackCount 4) 'Four-track validation'
Assert-Equal 6 (Assert-LiveTrackCount 6) 'Six-track validation'
Assert-Equal '' (Assert-DeviceAudioEndpoint '') 'Empty device endpoint disables LAN output'
Assert-Equal '192.168.31.88:4010' (Assert-DeviceAudioEndpoint '192.168.31.88:4010') `
    'Valid IPv4 UDP endpoint'
$playbackArgs = @(New-AudioHostArgumentList -Source '--playback-only' -LiveDirectory 'data\live' -TrackCount 6 -DeviceEndpoint '192.168.31.88:4010' -HopSeconds 3)
Assert-Equal 6 $playbackArgs.Count 'LAN playback must append endpoint and hop arguments'
Assert-Equal '192.168.31.88:4010' $playbackArgs[3] 'LAN playback endpoint argument'
Assert-Equal '--hop-seconds' $playbackArgs[4] 'LAN playback hop flag'
Assert-Equal 3 $playbackArgs[5] 'LAN playback hop value'
$localArgs = @(New-AudioHostArgumentList -Source '1234' -LiveDirectory 'data\live' -TrackCount 2)
Assert-Equal 5 $localArgs.Count 'Local playback must omit endpoint and retain default hop'
$systemArgs = @(New-AudioHostArgumentList -Source '--system-loopback' -LiveDirectory 'data\live' -TrackCount 2 -HopSeconds 3)
Assert-Equal '--system-loopback' $systemArgs[0] 'System loopback source argument'
Assert-Equal 3 $systemArgs[4] 'System loopback hop value'
Assert-Equal 'AboveNormal' (Get-StreamingPriorityClass) `
    'Native streaming hosts must outrank ordinary desktop build work'

$appleMusic = Get-SupportedMusicPlayerName 'AppleMusic.exe'
Assert-Equal 'AppleMusic' $appleMusic.ProcessName 'Apple Music must be a supported capture target'
Assert-Equal 'Apple Music' $appleMusic.DisplayName 'Apple Music display name'
$potPlayer = Get-SupportedMusicPlayerName 'PotPlayerMini64'
Assert-Equal 'PotPlayer' $potPlayer.DisplayName 'PotPlayer 64-bit must be supported'
Assert-Equal $null (Get-SupportedMusicPlayerName 'chrome.exe') `
    'Browsers must not be accepted as music capture targets'
Assert-Equal $null (Get-SupportedMusicPlayerName 'WindowsSystemAudio') `
    'System audio must not be accepted as a music capture target'
$selectedPlayer = Select-MusicPlayerProcess -PreferredProcessName 'Spotify' -Processes @(
    [pscustomobject]@{ Id = 30; ProcessName = 'Spotify'; MainWindowTitle = '' },
    [pscustomobject]@{ Id = 20; ProcessName = 'Spotify'; MainWindowTitle = 'Spotify Premium' },
    [pscustomobject]@{ Id = 10; ProcessName = 'chrome'; MainWindowTitle = 'Music' }
)
Assert-Equal 20 $selectedPlayer.Id 'Visible music player process must be preferred'
$fallbackPlayer = Select-MusicPlayerProcess -PreferredProcessName 'AppleMusic' -Processes @(
    [pscustomobject]@{ Id = 31; ProcessName = 'chrome'; MainWindowTitle = 'YouTube Music' },
    [pscustomobject]@{ Id = 22; ProcessName = 'Spotify'; MainWindowTitle = 'Spotify Premium' }
)
Assert-Equal 22 $fallbackPlayer.Id 'Any supported music player must be selected when the preferred player is absent'
$appleCaptureTarget = Resolve-MusicCaptureProcess -PlayerProcess (
    [pscustomobject]@{ Id = 40; ProcessName = 'AppleMusic'; SessionId = 1 }
) -Processes @(
    [pscustomobject]@{ Id = 40; ProcessName = 'AppleMusic'; SessionId = 1 },
    [pscustomobject]@{ Id = 41; ProcessName = 'AMPLibraryAgent'; SessionId = 2 },
    [pscustomobject]@{ Id = 42; ProcessName = 'AMPLibraryAgent'; SessionId = 1 }
)
Assert-Equal 42 $appleCaptureTarget.Id 'Apple Music capture must bind its audio agent in the same session'
$spotifyCaptureTarget = Resolve-MusicCaptureProcess -PlayerProcess (
    [pscustomobject]@{ Id = 50; ProcessName = 'Spotify'; SessionId = 1 }
) -Processes @(
    [pscustomobject]@{ Id = 50; ProcessName = 'Spotify'; SessionId = 1 },
    [pscustomobject]@{ Id = 42; ProcessName = 'AMPLibraryAgent'; SessionId = 1 }
)
Assert-Equal 50 $spotifyCaptureTarget.Id 'Other players must capture their own process'

$heartbeat = New-ControllerHeartbeat -ProcessId 1234
Assert-Equal 1 $heartbeat.version 'Controller heartbeat schema version'
Assert-Equal 1234 $heartbeat.pid 'Controller heartbeat process id'
if ([string]::IsNullOrWhiteSpace([string]$heartbeat.updated_at)) {
    throw 'Controller heartbeat timestamp must be present'
}

$unknownRejected = $false
try {
    Assert-LiveTrackCount 3 | Out-Null
} catch {
    $unknownRejected = $true
}
Assert-Equal $true $unknownRejected 'Unknown profile must be rejected'

$invalidEndpointRejected = $false
try {
    Assert-DeviceAudioEndpoint '192.168.031.88:4010' | Out-Null
} catch {
    $invalidEndpointRejected = $true
}
Assert-Equal $true $invalidEndpointRejected 'Device endpoint must be an IPv4 address and UDP port'

'HostController AirPlay idempotence tests: PASS'
