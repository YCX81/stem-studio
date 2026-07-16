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
    -RequestedProfile 'profile-two-track'
Assert-Equal $false $connected.RestartAirPlay 'Connected AirPlay must not restart'
Assert-Equal $false $connected.RestartPlayback 'Unchanged profile must not restart playback'

$changedProfile = Get-AirPlayStartPlan `
    -AirPlayRunning $true `
    -PlaybackRunning $true `
    -CurrentProfile 'profile-two-track' `
    -RequestedProfile 'profile-four-track'
Assert-Equal $false $changedProfile.RestartAirPlay 'Changing profile must preserve AirPlay'
Assert-Equal $true $changedProfile.RestartPlayback 'Changing profile must restart playback only'

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
Assert-Equal 'AboveNormal' (Get-StreamingPriorityClass) `
    'Native streaming hosts must outrank ordinary desktop build work'

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

'HostController AirPlay idempotence tests: PASS'
