function Get-AirPlayStartPlan {
    param(
        [Parameter(Mandatory = $true)][bool]$AirPlayRunning,
        [Parameter(Mandatory = $true)][bool]$PlaybackRunning,
        [AllowEmptyString()][string]$CurrentProfile,
        [Parameter(Mandatory = $true)][string]$RequestedProfile
    )

    $restartAirPlay = -not $AirPlayRunning
    [pscustomobject]@{
        RestartAirPlay = $restartAirPlay
        RestartPlayback = $restartAirPlay -or -not $PlaybackRunning -or $CurrentProfile -ne $RequestedProfile
    }
}

function Assert-LiveTrackCount {
    param([Parameter(Mandatory = $true)][int]$TrackCount)

    if ($TrackCount -notin @(2, 4, 6)) {
        throw "Unsupported live track count: $TrackCount"
    }
    return $TrackCount
}

function Get-StreamingPriorityClass {
    return 'AboveNormal'
}

function New-ControllerHeartbeat {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [DateTimeOffset]$Timestamp = [DateTimeOffset]::UtcNow
    )

    [pscustomobject]@{
        version = 1
        pid = $ProcessId
        updated_at = $Timestamp.ToString('o')
    }
}

function Set-StreamingProcessPriority {
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)

    try {
        $Process.PriorityClass = Get-StreamingPriorityClass
        return [string]$Process.PriorityClass
    } catch {
        return 'Unavailable'
    }
}
