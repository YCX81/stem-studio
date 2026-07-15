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
    -CurrentProfile '人声 / 伴奏 · 高质量' `
    -RequestedProfile '人声 / 伴奏 · 高质量'
Assert-Equal $false $connected.RestartAirPlay 'Connected AirPlay must not restart'
Assert-Equal $false $connected.RestartPlayback 'Unchanged profile must not restart playback'

$changedProfile = Get-AirPlayStartPlan `
    -AirPlayRunning $true `
    -PlaybackRunning $true `
    -CurrentProfile '人声 / 伴奏 · 高质量' `
    -RequestedProfile '四轨 · 人声/鼓/贝斯/其他'
Assert-Equal $false $changedProfile.RestartAirPlay 'Changing profile must preserve AirPlay'
Assert-Equal $true $changedProfile.RestartPlayback 'Changing profile must restart playback only'

$coldStart = Get-AirPlayStartPlan `
    -AirPlayRunning $false `
    -PlaybackRunning $false `
    -CurrentProfile '' `
    -RequestedProfile '六轨 · 加吉他/钢琴'
Assert-Equal $true $coldStart.RestartAirPlay 'Cold start must launch AirPlay'
Assert-Equal $true $coldStart.RestartPlayback 'Cold start must launch playback'

Assert-Equal 2 (Assert-LiveTrackCount 2) 'Two-track validation'
Assert-Equal 4 (Assert-LiveTrackCount 4) 'Four-track validation'
Assert-Equal 6 (Assert-LiveTrackCount 6) 'Six-track validation'
Assert-Equal 'AboveNormal' (Get-StreamingPriorityClass) `
    'Native streaming hosts must outrank ordinary desktop build work'

$unknownRejected = $false
try {
    Assert-LiveTrackCount 3 | Out-Null
} catch {
    $unknownRejected = $true
}
Assert-Equal $true $unknownRejected 'Unknown profile must be rejected'

'HostController AirPlay idempotence tests: PASS'
