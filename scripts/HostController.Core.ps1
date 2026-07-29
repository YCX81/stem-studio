function Get-AirPlayStartPlan {
    param(
        [Parameter(Mandatory = $true)][bool]$AirPlayRunning,
        [Parameter(Mandatory = $true)][bool]$PlaybackRunning,
        [AllowEmptyString()][string]$CurrentProfile,
        [Parameter(Mandatory = $true)][string]$RequestedProfile,
        [AllowEmptyString()][string]$CurrentDeviceEndpoint = '',
        [AllowEmptyString()][string]$RequestedDeviceEndpoint = '',
        [int]$CurrentHopSeconds = 6,
        [int]$RequestedHopSeconds = 6
    )

    $restartAirPlay = -not $AirPlayRunning
    [pscustomobject]@{
        RestartAirPlay = $restartAirPlay
        RestartPlayback = $restartAirPlay -or
            -not $PlaybackRunning -or
            $CurrentProfile -ne $RequestedProfile -or
            $CurrentDeviceEndpoint -ne $RequestedDeviceEndpoint -or
            $CurrentHopSeconds -ne $RequestedHopSeconds
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

function Assert-LiveHopSeconds {
    param([Parameter(Mandatory = $true)][int]$HopSeconds)

    if ($HopSeconds -notin @(3, 6)) {
        throw "Unsupported live hop: $HopSeconds"
    }
    return $HopSeconds
}

function New-AudioHostArgumentList {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$LiveDirectory,
        [Parameter(Mandatory = $true)][int]$TrackCount,
        [AllowEmptyString()][string]$DeviceEndpoint = '',
        [int]$HopSeconds = 6
    )

    $arguments = @($Source, $LiveDirectory, (Assert-LiveTrackCount $TrackCount))
    $validatedEndpoint = Assert-DeviceAudioEndpoint $DeviceEndpoint
    if ($validatedEndpoint) {
        $arguments += $validatedEndpoint
    }
    $arguments += @('--hop-seconds', (Assert-LiveHopSeconds $HopSeconds))
    return $arguments
}

function Get-StreamingPriorityClass {
    return 'AboveNormal'
}

function Get-SupportedMusicPlayerName {
    param([Parameter(Mandatory = $true)][string]$ProcessName)

    $candidate = [System.IO.Path]::GetFileNameWithoutExtension($ProcessName).Trim()
    $supportedPlayers = [ordered]@{
        'AppleMusic' = 'Apple Music'
        'Music' = 'Music'
        'Spotify' = 'Spotify'
        'cloudmusic' = 'NetEase Cloud Music'
        'QQMusic' = 'QQ Music'
        'foobar2000' = 'foobar2000'
        'AIMP' = 'AIMP'
        'MusicBee' = 'MusicBee'
        'vlc' = 'VLC'
        'PotPlayerMini' = 'PotPlayer'
        'PotPlayerMini64' = 'PotPlayer'
        'iTunes' = 'iTunes'
        'wmplayer' = 'Windows Media Player'
        'Microsoft.Media.Player' = 'Windows Media Player'
    }
    foreach ($entry in $supportedPlayers.GetEnumerator()) {
        if ($candidate.Equals($entry.Key, [System.StringComparison]::OrdinalIgnoreCase)) {
            return [pscustomobject]@{
                ProcessName = $entry.Key
                DisplayName = $entry.Value
            }
        }
    }
    return $null
}

function Select-MusicPlayerProcess {
    param(
        [Parameter(Mandatory = $true)][object[]]$Processes,
        [AllowEmptyString()][string]$PreferredProcessName = ''
    )

    $preferred = [System.IO.Path]::GetFileNameWithoutExtension($PreferredProcessName)
    return $Processes |
        Where-Object {
            $null -ne (Get-SupportedMusicPlayerName ([string]$_.ProcessName))
        } |
        Sort-Object `
            @{ Expression = {
                $candidate = [System.IO.Path]::GetFileNameWithoutExtension([string]$_.ProcessName)
                if ($preferred -and $candidate.Equals($preferred, [System.StringComparison]::OrdinalIgnoreCase)) { 0 } else { 1 }
            } }, `
            @{ Expression = { if ($_.MainWindowTitle) { 0 } else { 1 } } }, `
            Id |
        Select-Object -First 1
}

function Resolve-MusicCaptureProcess {
    param(
        [Parameter(Mandatory = $true)]$PlayerProcess,
        [Parameter(Mandatory = $true)][object[]]$Processes
    )

    $playerName = [System.IO.Path]::GetFileNameWithoutExtension(
        [string]$PlayerProcess.ProcessName
    )
    if ($playerName.Equals('AppleMusic', [System.StringComparison]::OrdinalIgnoreCase)) {
        $appleAudioProcess = $Processes |
            Where-Object {
                ([System.IO.Path]::GetFileNameWithoutExtension(
                    [string]$_.ProcessName
                )).Equals(
                    'AMPLibraryAgent',
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -and (
                    $null -eq $PlayerProcess.PSObject.Properties['SessionId'] -or
                    $null -eq $_.PSObject.Properties['SessionId'] -or
                    [int]$_.SessionId -eq [int]$PlayerProcess.SessionId
                )
            } |
            Sort-Object Id |
            Select-Object -First 1
        if ($null -ne $appleAudioProcess) {
            return $appleAudioProcess
        }
    }
    return $PlayerProcess
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
