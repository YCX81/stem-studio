$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot '..\scripts\BufferedAppUpdate.Core.ps1')

$updateScript = Join-Path $PSScriptRoot '..\scripts\Update-AppBuffered.ps1'
$tokens = $null
$parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    $updateScript,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {
    throw "Update-AppBuffered.ps1 has syntax errors: $($parseErrors -join '; ')"
}
$updateSource = Get-Content -LiteralPath $updateScript -Raw -Encoding UTF8
$planOnlyIndex = $updateSource.IndexOf('if ($PlanOnly)', [StringComparison]::Ordinal)
$candidateTagIndex = $updateSource.IndexOf(
    "'image', 'tag', `$candidateImageId, 'stem-studio:candidate-local'",
    [StringComparison]::Ordinal
)
$rollbackTagIndex = $updateSource.IndexOf(
    "'image', 'tag', `$runningImageId, 'stem-studio:rollback-local'",
    [StringComparison]::Ordinal
)
$replaceIndex = $updateSource.IndexOf(
    "'compose', 'up', '--detach', '--no-deps', '--force-recreate', 'app'",
    [StringComparison]::Ordinal
)
if ($planOnlyIndex -lt 0 -or $candidateTagIndex -lt 0 -or
    $rollbackTagIndex -lt 0 -or $replaceIndex -lt 0) {
    throw 'Buffered update script no longer exposes plan, image tags, and app replacement.'
}
if ($planOnlyIndex -ge $candidateTagIndex -or
    $candidateTagIndex -ge $rollbackTagIndex -or
    $rollbackTagIndex -ge $replaceIndex) {
    throw 'Plan-only return and rollback image tags must precede container replacement.'
}
if ($updateSource.IndexOf('[long]$gpu.cache_misses -eq 0L', [StringComparison]::Ordinal) -lt 0) {
    throw 'Replacement readiness must require zero cache misses.'
}

function Assert-Equal($Expected, $Actual, [string]$Message) {
    if ($Expected -ne $Actual) {
        throw "$Message (expected=$Expected, actual=$Actual)"
    }
}

function Assert-True([bool]$Value, [string]$Message) {
    if (-not $Value) { throw $Message }
}

function New-TestSnapshot {
    param(
        [double]$BufferSeconds = 18.0,
        [bool]$CacheHit = $true,
        [int]$CacheHits = 145,
        [int]$CacheMisses = 483,
        [long]$PcmFrames = 10000,
        [long]$PlaybackSequence = 2700,
        [int]$Underruns = 0,
        [int]$DeviceRecoveries = 1,
        [int]$SkippedSequence = 0,
        [bool]$DeviceRecovering = $false
    )

    return [pscustomobject]@{
        AirPlay = [pscustomobject]@{
            state = 'streaming'
            pcm_frames = $PcmFrames
        }
        Gpu = [pscustomobject]@{
            state = 'running'
            cache_hit = $CacheHit
            cache_hits = $CacheHits
            cache_misses = $CacheMisses
            fallback_windows = 0
        }
        Playback = [pscustomobject]@{
            state = 'playing'
            sequence = $PlaybackSequence
            queued_sequence = $PlaybackSequence + 2
            buffered_seconds = $BufferSeconds
            underruns = $Underruns
            device_recoveries = $DeviceRecoveries
            device_recovering = $DeviceRecovering
            skipped_sequence = $SkippedSequence
        }
    }
}

$readySnapshot = New-TestSnapshot
$ready = Get-BufferedAppUpdateReadiness -Snapshot $readySnapshot -MinimumBufferSeconds 15
Assert-Equal $true $ready.Ready 'Cached playback with sufficient buffer must be deployable'
Assert-Equal 'Ready' $ready.Code 'Ready snapshot code'

$lowBuffer = Get-BufferedAppUpdateReadiness `
    -Snapshot (New-TestSnapshot -BufferSeconds 14.99) `
    -MinimumBufferSeconds 15
Assert-Equal $false $lowBuffer.Ready 'Low buffer must block an online update'
Assert-Equal 'LowBuffer' $lowBuffer.Code 'Low buffer rejection code'

$uncached = Get-BufferedAppUpdateReadiness `
    -Snapshot (New-TestSnapshot -CacheHit $false) `
    -MinimumBufferSeconds 15
Assert-Equal $false $uncached.Ready 'GPU inference playback must block an online update'
Assert-Equal 'NotCachedPlayback' $uncached.Code 'Uncached playback rejection code'

$recovering = Get-BufferedAppUpdateReadiness `
    -Snapshot (New-TestSnapshot -DeviceRecovering $true) `
    -MinimumBufferSeconds 15
Assert-Equal $false $recovering.Ready 'A recovering output device must block update'
Assert-Equal 'DeviceRecovering' $recovering.Code 'Device recovery rejection code'

$windowEvents = New-Object System.Collections.Generic.List[string]
$windowSnapshots = New-Object System.Collections.Generic.Queue[object]
$windowSnapshots.Enqueue((New-TestSnapshot -BufferSeconds 13))
$windowSnapshots.Enqueue($readySnapshot)
$window = Wait-BufferedAppUpdateWindow `
    -GetSnapshot {
        $windowEvents.Add('snapshot') | Out-Null
        $windowSnapshots.Dequeue()
    } `
    -Sleep {
        param($Milliseconds)
        $windowEvents.Add("sleep:$Milliseconds") | Out-Null
    } `
    -MinimumBufferSeconds 15 `
    -MaximumAttempts 2 `
    -PollMilliseconds 250
Assert-Equal $true $window.Ready 'The waiter must capture the next safe buffer crest'
Assert-Equal 2 $window.Attempts 'Safe-window attempt count'
Assert-Equal 'snapshot,sleep:250,snapshot' ($windowEvents -join ',') `
    'Safe-window polling order'

$windowEvents.Clear()
$windowSnapshots = New-Object System.Collections.Generic.Queue[object]
$windowSnapshots.Enqueue((New-TestSnapshot -DeviceRecovering $true))
$fatalWindow = Wait-BufferedAppUpdateWindow `
    -GetSnapshot {
        $windowEvents.Add('snapshot') | Out-Null
        $windowSnapshots.Dequeue()
    } `
    -Sleep {
        param($Milliseconds)
        $windowEvents.Add("sleep:$Milliseconds") | Out-Null
    } `
    -MinimumBufferSeconds 15 `
    -MaximumAttempts 3 `
    -PollMilliseconds 250
Assert-Equal $false $fatalWindow.Ready 'A non-transient device fault must not be retried'
Assert-Equal 'DeviceRecovering' $fatalWindow.Code 'Fatal safe-window code'
Assert-Equal 1 $fatalWindow.Attempts 'Fatal readiness must stop on the first attempt'
Assert-Equal 'snapshot' ($windowEvents -join ',') 'Fatal readiness must not sleep'

$afterSuccess = New-TestSnapshot `
    -BufferSeconds 12 `
    -CacheHits 2 `
    -CacheMisses 0 `
    -PcmFrames 20000 `
    -PlaybackSequence 2704
$continuity = Test-BufferedAppUpdateContinuity `
    -Before $readySnapshot `
    -After $afterSuccess
Assert-Equal $true $continuity.Passed 'Healthy cached restart must preserve continuity'
Assert-Equal 'Passed' $continuity.Code 'Continuity pass code'

$afterUnderrun = New-TestSnapshot `
    -BufferSeconds 12 `
    -CacheHits 2 `
    -CacheMisses 0 `
    -PcmFrames 20000 `
    -PlaybackSequence 2704 `
    -Underruns 1
$underrun = Test-BufferedAppUpdateContinuity `
    -Before $readySnapshot `
    -After $afterUnderrun
Assert-Equal $false $underrun.Passed 'An underrun during replacement must fail deployment'
Assert-Equal 'UnderrunChanged' $underrun.Code 'Underrun failure code'

$afterMiss = New-TestSnapshot `
    -BufferSeconds 12 `
    -CacheHits 1 `
    -CacheMisses 1 `
    -PcmFrames 20000 `
    -PlaybackSequence 2704
$miss = Test-BufferedAppUpdateContinuity -Before $readySnapshot -After $afterMiss
Assert-Equal $false $miss.Passed 'The replacement worker must not re-run GPU on cached playback'
Assert-Equal 'CacheMissAfterUpdate' $miss.Code 'Post-update cache miss failure code'

$events = New-Object System.Collections.Generic.List[string]
$snapshots = New-Object System.Collections.Generic.Queue[object]
$snapshots.Enqueue($readySnapshot)
$snapshots.Enqueue($afterSuccess)
$success = Invoke-BufferedAppUpdateTransaction `
    -GetSnapshot { $events.Add('snapshot') | Out-Null; $snapshots.Dequeue() } `
    -Replace { $events.Add('replace') | Out-Null } `
    -WaitHealthy { $events.Add('health') | Out-Null; $true } `
    -Rollback { $events.Add('rollback') | Out-Null } `
    -MinimumBufferSeconds 15
Assert-Equal 'succeeded' $success.State 'Healthy transaction must succeed'
Assert-Equal 'snapshot,replace,health,snapshot' ($events -join ',') `
    'Successful transaction order'

$events.Clear()
$snapshots = New-Object System.Collections.Generic.Queue[object]
$snapshots.Enqueue($readySnapshot)
$unhealthy = Invoke-BufferedAppUpdateTransaction `
    -GetSnapshot { $events.Add('snapshot') | Out-Null; $snapshots.Dequeue() } `
    -Replace { $events.Add('replace') | Out-Null } `
    -WaitHealthy { $events.Add('health') | Out-Null; $false } `
    -Rollback { $events.Add('rollback') | Out-Null } `
    -MinimumBufferSeconds 15
Assert-Equal 'rolled_back' $unhealthy.State 'Unhealthy replacement must roll back'
Assert-Equal 'snapshot,replace,health,rollback' ($events -join ',') `
    'Health failure rollback order'

$events.Clear()
$snapshots = New-Object System.Collections.Generic.Queue[object]
$snapshots.Enqueue($readySnapshot)
$snapshots.Enqueue($afterMiss)
$continuityFailure = Invoke-BufferedAppUpdateTransaction `
    -GetSnapshot { $events.Add('snapshot') | Out-Null; $snapshots.Dequeue() } `
    -Replace { $events.Add('replace') | Out-Null } `
    -WaitHealthy { $events.Add('health') | Out-Null; $true } `
    -Rollback { $events.Add('rollback') | Out-Null } `
    -MinimumBufferSeconds 15
Assert-Equal 'rolled_back' $continuityFailure.State `
    'Post-update cache miss must roll back'
Assert-Equal 'CacheMissAfterUpdate' $continuityFailure.Code `
    'Transaction must preserve continuity failure code'
Assert-Equal 'snapshot,replace,health,snapshot,rollback' ($events -join ',') `
    'Continuity failure rollback order'

$events.Clear()
$snapshots = New-Object System.Collections.Generic.Queue[object]
$snapshots.Enqueue((New-TestSnapshot -BufferSeconds 2))
$blocked = Invoke-BufferedAppUpdateTransaction `
    -GetSnapshot { $events.Add('snapshot') | Out-Null; $snapshots.Dequeue() } `
    -Replace { $events.Add('replace') | Out-Null } `
    -WaitHealthy { $events.Add('health') | Out-Null; $true } `
    -Rollback { $events.Add('rollback') | Out-Null } `
    -MinimumBufferSeconds 15
Assert-Equal 'blocked' $blocked.State 'Unsafe baseline must block before replacement'
Assert-Equal 'snapshot' ($events -join ',') 'Blocked transaction must not mutate Docker state'

'Buffered app update core tests: PASS'
