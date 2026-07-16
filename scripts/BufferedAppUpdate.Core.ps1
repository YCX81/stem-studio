function New-BufferedAppUpdateCheck {
    param(
        [Parameter(Mandatory = $true)][bool]$Passed,
        [Parameter(Mandatory = $true)][string]$Code,
        [string]$Message = ''
    )

    return [pscustomobject]@{
        Passed = $Passed
        Ready = $Passed
        Code = $Code
        Message = $Message
    }
}

function Get-BufferedAppUpdateReadiness {
    param(
        [Parameter(Mandatory = $true)]$Snapshot,
        [ValidateRange(1.0, 120.0)][double]$MinimumBufferSeconds = 15.0,
        [ValidateRange(0, 16)][int]$MaximumQueuedWindows = 3
    )

    if ([string]$Snapshot.AirPlay.state -ne 'streaming') {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'StreamInactive' `
            -Message 'AirPlay must be streaming before a buffered update.'
    }
    if ([string]$Snapshot.Gpu.state -ne 'running') {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'GpuWorkerNotReady' `
            -Message 'The GPU worker must be running before replacement.'
    }
    if (-not [bool]$Snapshot.Gpu.cache_hit) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'NotCachedPlayback' `
            -Message 'Online replacement is allowed only while a song-cache hit is playing.'
    }
    if ([string]$Snapshot.Playback.state -ne 'playing') {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'PlaybackInactive' `
            -Message 'The native playback host must be actively rendering audio.'
    }
    if ([bool]$Snapshot.Playback.device_recovering) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'DeviceRecovering' `
            -Message 'The output device is recovering and cannot protect an online update.'
    }
    $bufferedSeconds = [double]$Snapshot.Playback.buffered_seconds
    if ($bufferedSeconds -lt $MinimumBufferSeconds) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'LowBuffer' `
            -Message "Playback buffer is $bufferedSeconds seconds; $MinimumBufferSeconds seconds are required."
    }
    $pendingWindows = [Math]::Max(
        0L,
        [long]$Snapshot.Playback.queued_sequence - [long]$Snapshot.Playback.sequence
    )
    if ($pendingWindows -gt $MaximumQueuedWindows) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'PlaybackBacklog' `
            -Message "Playback has $pendingWindows queued windows; at most $MaximumQueuedWindows are allowed."
    }
    return New-BufferedAppUpdateCheck -Passed $true -Code 'Ready' `
        -Message 'Cached playback has enough reserve for an online app replacement.'
}

function Wait-BufferedAppUpdateWindow {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$GetSnapshot,
        [scriptblock]$Sleep = {
            param($Milliseconds)
            Start-Sleep -Milliseconds $Milliseconds
        },
        [ValidateRange(1.0, 120.0)][double]$MinimumBufferSeconds = 15.0,
        [ValidateRange(1, 2400)][int]$MaximumAttempts = 1,
        [ValidateRange(10, 5000)][int]$PollMilliseconds = 250,
        [ValidateRange(0, 16)][int]$MaximumQueuedWindows = 3
    )

    $transientCodes = @('LowBuffer', 'NotCachedPlayback', 'PlaybackBacklog')
    for ($attempt = 1; $attempt -le $MaximumAttempts; $attempt++) {
        $snapshot = & $GetSnapshot
        $readiness = Get-BufferedAppUpdateReadiness `
            -Snapshot $snapshot `
            -MinimumBufferSeconds $MinimumBufferSeconds `
            -MaximumQueuedWindows $MaximumQueuedWindows
        if ($readiness.Ready -or
            $readiness.Code -notin $transientCodes -or
            $attempt -eq $MaximumAttempts) {
            return [pscustomobject]@{
                Ready = $readiness.Ready
                Code = $readiness.Code
                Message = $readiness.Message
                Snapshot = $snapshot
                Attempts = $attempt
            }
        }
        & $Sleep $PollMilliseconds | Out-Null
    }
}

function Test-BufferedAppUpdateContinuity {
    param(
        [Parameter(Mandatory = $true)]$Before,
        [Parameter(Mandatory = $true)]$After
    )

    if ([long]$After.Playback.underruns -ne [long]$Before.Playback.underruns) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'UnderrunChanged' `
            -Message 'The native playback underrun counter changed during replacement.'
    }
    if ([long]$After.Playback.device_recoveries -ne [long]$Before.Playback.device_recoveries) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'DeviceRecoveryChanged' `
            -Message 'The output device recovery counter changed during replacement.'
    }
    if ([long]$After.Playback.skipped_sequence -ne [long]$Before.Playback.skipped_sequence) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'SkippedSequenceChanged' `
            -Message 'The native playback host skipped a result window during replacement.'
    }
    if ([bool]$After.Playback.device_recovering) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'DeviceRecoveringAfterUpdate' `
            -Message 'The output device is still recovering after replacement.'
    }
    if ([string]$After.Playback.state -ne 'playing') {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'PlaybackStopped' `
            -Message 'Native playback stopped during replacement.'
    }
    if ([double]$After.Playback.buffered_seconds -le 0.0) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'BufferDrained' `
            -Message 'The protected playback buffer drained during replacement.'
    }
    if ([long]$After.Playback.sequence -lt [long]$Before.Playback.sequence) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'PlaybackSequenceRegressed' `
            -Message 'The native playback sequence regressed during replacement.'
    }
    if ([string]$After.AirPlay.state -ne 'streaming' -or
        [long]$After.AirPlay.pcm_frames -le [long]$Before.AirPlay.pcm_frames) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'AirPlayDidNotAdvance' `
            -Message 'AirPlay PCM did not continue across replacement.'
    }
    if ([string]$After.Gpu.state -ne 'running' -or -not [bool]$After.Gpu.cache_hit) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'CachePlaybackNotRestored' `
            -Message 'The replacement worker did not resume cached playback.'
    }
    if ([long]$After.Gpu.cache_misses -ne 0L) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'CacheMissAfterUpdate' `
            -Message 'The replacement worker re-ran GPU inference during cached playback.'
    }
    if ([long]$After.Gpu.cache_hits -le 0L) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'NoCacheHitAfterUpdate' `
            -Message 'The replacement worker has not published a cache hit.'
    }
    if ([long]$After.Gpu.fallback_windows -ne 0L) {
        return New-BufferedAppUpdateCheck -Passed $false -Code 'FallbackAfterUpdate' `
            -Message 'The replacement worker used fallback audio.'
    }
    return New-BufferedAppUpdateCheck -Passed $true -Code 'Passed' `
        -Message 'AirPlay, cached six-track processing, and native playback remained continuous.'
}

function New-BufferedAppUpdateTransactionResult {
    param(
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $true)][string]$Code,
        [string]$Message = '',
        $Before = $null,
        $After = $null
    )

    return [pscustomobject]@{
        State = $State
        Code = $Code
        Message = $Message
        Before = $Before
        After = $After
    }
}

function Invoke-BufferedAppUpdateTransaction {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$GetSnapshot,
        [Parameter(Mandatory = $true)][scriptblock]$Replace,
        [Parameter(Mandatory = $true)][scriptblock]$WaitHealthy,
        [Parameter(Mandatory = $true)][scriptblock]$Rollback,
        [ValidateRange(1.0, 120.0)][double]$MinimumBufferSeconds = 15.0,
        [ValidateRange(0, 16)][int]$MaximumQueuedWindows = 3
    )

    $before = & $GetSnapshot
    $readiness = Get-BufferedAppUpdateReadiness `
        -Snapshot $before `
        -MinimumBufferSeconds $MinimumBufferSeconds `
        -MaximumQueuedWindows $MaximumQueuedWindows
    if (-not $readiness.Ready) {
        return New-BufferedAppUpdateTransactionResult `
            -State 'blocked' `
            -Code $readiness.Code `
            -Message $readiness.Message `
            -Before $before
    }

    $after = $null
    $failureCode = 'ReplacementFailed'
    $failureMessage = ''
    try {
        & $Replace | Out-Null
        $healthOutput = @(& $WaitHealthy)
        $healthy = $healthOutput.Count -gt 0 -and [bool]$healthOutput[-1]
        if (-not $healthy) {
            $failureCode = 'ContainerUnhealthy'
            throw 'The replacement app container did not become healthy in time.'
        }
        $after = & $GetSnapshot
        $continuity = Test-BufferedAppUpdateContinuity -Before $before -After $after
        if (-not $continuity.Passed) {
            $failureCode = $continuity.Code
            throw $continuity.Message
        }
        return New-BufferedAppUpdateTransactionResult `
            -State 'succeeded' `
            -Code 'Passed' `
            -Message $continuity.Message `
            -Before $before `
            -After $after
    } catch {
        $failureMessage = $_.Exception.Message
        try {
            & $Rollback | Out-Null
        } catch {
            return New-BufferedAppUpdateTransactionResult `
                -State 'rollback_failed' `
                -Code $failureCode `
                -Message "$failureMessage Rollback also failed: $($_.Exception.Message)" `
                -Before $before `
                -After $after
        }
        return New-BufferedAppUpdateTransactionResult `
            -State 'rolled_back' `
            -Code $failureCode `
            -Message $failureMessage `
            -Before $before `
            -After $after
    }
}
