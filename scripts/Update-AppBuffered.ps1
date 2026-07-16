param(
    [string]$Root = (Join-Path $PSScriptRoot '..'),
    [ValidateRange(1.0, 120.0)][double]$MinimumBufferSeconds = 15.0,
    [ValidateRange(0, 600)][int]$SafeWindowTimeoutSeconds = 30,
    [ValidateRange(50, 5000)][int]$SafeWindowPollMilliseconds = 250,
    [ValidateRange(5, 180)][int]$HealthTimeoutSeconds = 60,
    [ValidateRange(5, 180)][int]$PostUpdateTimeoutSeconds = 45,
    [switch]$PlanOnly
)

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'BufferedAppUpdate.Core.ps1')

function Read-LiveJsonRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [ValidateRange(1, 100)][int]$Attempts = 40
    )

    $lastError = $null
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        try {
            return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            $lastError = $_
            Start-Sleep -Milliseconds 25
        }
    }
    throw "Unable to read live status ${Path}: $($lastError.Exception.Message)"
}

function Get-LiveUpdateSnapshot {
    param([Parameter(Mandatory = $true)][string]$WorkspaceRoot)

    $liveRoot = Join-Path $WorkspaceRoot 'data\live'
    return [pscustomobject]@{
        AirPlay = Read-LiveJsonRetry -Path (Join-Path $liveRoot 'airplay-status.json')
        Gpu = Read-LiveJsonRetry -Path (Join-Path $liveRoot 'gpu-status.json')
        Playback = Read-LiveJsonRetry -Path (Join-Path $liveRoot 'playback-status.json')
    }
}

function Invoke-DockerChecked {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = @(& docker @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed: $($output -join [Environment]::NewLine)"
    }
    return $output
}

function Get-AppContainerId {
    $output = @(Invoke-DockerChecked -Arguments @('compose', 'ps', '--quiet', 'app'))
    $candidate = @($output | Where-Object { [string]$_ -match '^[0-9a-f]{12,64}$' }) |
        Select-Object -Last 1
    return [string]$candidate
}

function Get-ContainerImageId {
    param([Parameter(Mandatory = $true)][string]$ContainerId)

    $output = @(Invoke-DockerChecked -Arguments @(
        'inspect',
        '--format',
        '{{.Image}}',
        $ContainerId
    ))
    $candidate = @($output | Where-Object { [string]$_ -match '^sha256:[0-9a-f]{64}$' }) |
        Select-Object -Last 1
    if (-not $candidate) { throw 'Docker did not return the running app image ID.' }
    return [string]$candidate
}

function Get-ImageId {
    param([Parameter(Mandatory = $true)][string]$Image)

    $output = @(Invoke-DockerChecked -Arguments @(
        'image',
        'inspect',
        '--format',
        '{{.Id}}',
        $Image
    ))
    $candidate = @($output | Where-Object { [string]$_ -match '^sha256:[0-9a-f]{64}$' }) |
        Select-Object -Last 1
    if (-not $candidate) { throw "Docker image was not found: $Image" }
    return [string]$candidate
}

function Wait-AppContainerHealthy {
    param([ValidateRange(5, 180)][int]$TimeoutSeconds)

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $containerId = Get-AppContainerId
            if ($containerId) {
                $output = @(Invoke-DockerChecked -Arguments @(
                    'inspect',
                    '--format',
                    '{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}',
                    $containerId
                ))
                if (@($output | Where-Object { [string]$_ -eq 'true|healthy' }).Count -gt 0) {
                    return $true
                }
            }
        } catch {
            # A container can disappear briefly while Compose replaces it.
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Wait-ReplacementWorkerReady {
    param(
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
        [Parameter(Mandatory = $true)][DateTime]$StartedAtUtc,
        [ValidateRange(5, 180)][int]$HealthTimeout,
        [ValidateRange(5, 180)][int]$WorkerTimeout
    )

    if (-not (Wait-AppContainerHealthy -TimeoutSeconds $HealthTimeout)) {
        return $false
    }
    $gpuStatusPath = Join-Path $WorkspaceRoot 'data\live\gpu-status.json'
    $deadline = [DateTime]::UtcNow.AddSeconds($WorkerTimeout)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $statusFile = Get-Item -LiteralPath $gpuStatusPath
            $gpu = Read-LiveJsonRetry -Path $gpuStatusPath
            if ($statusFile.LastWriteTimeUtc -gt $StartedAtUtc -and
                [string]$gpu.state -eq 'running' -and
                [bool]$gpu.cache_hit -and
                [long]$gpu.cache_hits -gt 0L -and
                [long]$gpu.cache_misses -eq 0L -and
                [long]$gpu.fallback_windows -eq 0L) {
                return $true
            }
        } catch {
            # Status replacement is atomic but readers can race the rename.
        }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Write-BufferedUpdateState {
    param(
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
        [Parameter(Mandatory = $true)]$Payload
    )

    $directory = Join-Path $WorkspaceRoot 'data\temp'
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $destination = Join-Path $directory 'buffered-app-update-state.json'
    $partial = "$destination.part"
    $Payload | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $partial -Encoding UTF8
    Move-Item -LiteralPath $partial -Destination $destination -Force
}

$workspace = [System.IO.Path]::GetFullPath($Root)
$previousLocation = Get-Location
try {
    Set-Location -LiteralPath $workspace
    $containerId = Get-AppContainerId
    if (-not $containerId) { throw 'The Stem Studio app container is not running.' }
    $runningImageId = Get-ContainerImageId -ContainerId $containerId
    $candidateImageId = Get-ImageId -Image 'stem-studio:0.1.0'
    $maximumWindowAttempts = [Math]::Max(
        1,
        [int][Math]::Floor(
            ($SafeWindowTimeoutSeconds * 1000.0) / $SafeWindowPollMilliseconds
        ) + 1
    )
    $window = Wait-BufferedAppUpdateWindow `
        -GetSnapshot { Get-LiveUpdateSnapshot -WorkspaceRoot $workspace } `
        -MinimumBufferSeconds $MinimumBufferSeconds `
        -MaximumAttempts $maximumWindowAttempts `
        -PollMilliseconds $SafeWindowPollMilliseconds
    $snapshot = $window.Snapshot
    $readiness = [pscustomobject]@{
        Ready = $window.Ready
        Code = $window.Code
        Message = $window.Message
    }

    $plan = [ordered]@{
        state = $(if ($readiness.Ready) { 'ready' } else { 'blocked' })
        code = $readiness.Code
        message = $readiness.Message
        plan_only = [bool]$PlanOnly
        running_image_id = $runningImageId
        candidate_image_id = $candidateImageId
        image_change_required = $runningImageId -ne $candidateImageId
        minimum_buffer_seconds = $MinimumBufferSeconds
        observed_buffer_seconds = [double]$snapshot.Playback.buffered_seconds
        observed_cache_hit = [bool]$snapshot.Gpu.cache_hit
        safe_window_attempts = $window.Attempts
    }
    if ($PlanOnly) {
        $plan | ConvertTo-Json -Depth 8
        return
    }
    if (-not $readiness.Ready) {
        throw "Buffered app update is blocked: $($readiness.Code) - $($readiness.Message)"
    }
    if ($runningImageId -eq $candidateImageId) {
        $plan.state = 'already_current'
        $plan | ConvertTo-Json -Depth 8
        return
    }

    $operation = [pscustomobject]@{ StartedAtUtc = [DateTime]::MinValue }
    $transaction = Invoke-BufferedAppUpdateTransaction `
        -GetSnapshot { Get-LiveUpdateSnapshot -WorkspaceRoot $workspace } `
        -Replace {
            $operation.StartedAtUtc = [DateTime]::UtcNow
            Invoke-DockerChecked -Arguments @(
                'image', 'tag', $candidateImageId, 'stem-studio:candidate-local'
            ) | Out-Null
            Invoke-DockerChecked -Arguments @(
                'image', 'tag', $runningImageId, 'stem-studio:rollback-local'
            ) | Out-Null
            Invoke-DockerChecked -Arguments @(
                'compose', 'up', '--detach', '--no-deps', '--force-recreate', 'app'
            ) | Out-Null
        } `
        -WaitHealthy {
            Wait-ReplacementWorkerReady `
                -WorkspaceRoot $workspace `
                -StartedAtUtc $operation.StartedAtUtc `
                -HealthTimeout $HealthTimeoutSeconds `
                -WorkerTimeout $PostUpdateTimeoutSeconds
        } `
        -Rollback {
            Invoke-DockerChecked -Arguments @(
                'image', 'tag', 'stem-studio:rollback-local', 'stem-studio:0.1.0'
            ) | Out-Null
            Invoke-DockerChecked -Arguments @(
                'compose', 'up', '--detach', '--no-deps', '--force-recreate', 'app'
            ) | Out-Null
            if (-not (Wait-AppContainerHealthy -TimeoutSeconds $HealthTimeoutSeconds)) {
                throw 'The rollback app container did not become healthy.'
            }
            Invoke-DockerChecked -Arguments @(
                'image', 'tag', 'stem-studio:candidate-local', 'stem-studio:0.1.0'
            ) | Out-Null
        } `
        -MinimumBufferSeconds $MinimumBufferSeconds

    $state = [ordered]@{
        state = $transaction.State
        code = $transaction.Code
        message = $transaction.Message
        completed_at_utc = [DateTime]::UtcNow.ToString('o')
        running_image_id_before = $runningImageId
        candidate_image_id = $candidateImageId
        before = $transaction.Before
        after = $transaction.After
    }
    Write-BufferedUpdateState -WorkspaceRoot $workspace -Payload $state
    $state | ConvertTo-Json -Depth 12
    if ($transaction.State -ne 'succeeded') {
        throw "Buffered app update did not succeed: $($transaction.State) / $($transaction.Code)"
    }
} finally {
    Set-Location -LiteralPath $previousLocation
}
