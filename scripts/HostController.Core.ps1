function Get-AirPlayStartPlan {
    param(
        [Parameter(Mandatory = $true)][bool]$AirPlayRunning,
        [Parameter(Mandatory = $true)][bool]$PlaybackRunning,
        [AllowEmptyString()][string]$CurrentProfile,
        [Parameter(Mandatory = $true)][string]$RequestedProfile,
        [AllowEmptyString()][string]$CurrentDeviceEndpoint = '',
        [AllowEmptyString()][string]$RequestedDeviceEndpoint = ''
    )

    $restartAirPlay = -not $AirPlayRunning
    [pscustomobject]@{
        RestartAirPlay = $restartAirPlay
        RestartPlayback = $restartAirPlay -or
            -not $PlaybackRunning -or
            $CurrentProfile -ne $RequestedProfile -or
            $CurrentDeviceEndpoint -ne $RequestedDeviceEndpoint
    }
}

function Assert-DeviceAudioEndpoint {
    param([AllowEmptyString()][string]$Endpoint = '')

    $candidate = $Endpoint.Trim()
    if ([string]::IsNullOrEmpty($candidate)) {
        return ''
    }
    if ($candidate -notmatch '^([^:]+):([0-9]+)$') {
        throw "Invalid device audio endpoint: $Endpoint"
    }

    $addressText = $Matches[1]
    $portText = $Matches[2]
    $address = $null
    $port = 0
    $strictAddress = $addressText -match '^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$'
    if (-not $strictAddress -or
        -not [System.Net.IPAddress]::TryParse($addressText, [ref]$address) -or
        $address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        -not [int]::TryParse($portText, [ref]$port) -or
        $port -lt 1 -or $port -gt 65535) {
        throw "Invalid device audio endpoint: $Endpoint"
    }
    return "$($address.ToString()):$port"
}

function Assert-LiveTrackCount {
    param([Parameter(Mandatory = $true)][int]$TrackCount)

    if ($TrackCount -notin @(2, 4, 6)) {
        throw "Unsupported live track count: $TrackCount"
    }
    return $TrackCount
}

function New-AudioHostArgumentList {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$LiveDirectory,
        [Parameter(Mandatory = $true)][int]$TrackCount,
        [AllowEmptyString()][string]$DeviceEndpoint = ''
    )

    $arguments = @($Source, $LiveDirectory, (Assert-LiveTrackCount $TrackCount))
    $validatedEndpoint = Assert-DeviceAudioEndpoint $DeviceEndpoint
    if ($validatedEndpoint) {
        $arguments += $validatedEndpoint
    }
    return $arguments
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
