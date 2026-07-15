param(
    [string]$Root = '',
    [string]$AudioCandidate = '',
    [string]$AirPlayCandidateRoot = '',
    [string]$ExpectedAudioHash = '',
    [string]$ExpectedAirPlayHash = '',
    [ValidateRange(1, 30)][int]$QuietProbeSeconds = 5,
    [ValidateRange(1, 15)][int]$NativeStopTimeoutSeconds = 10,
    [ValidateRange(5, 90)][int]$ControllerTimeoutSeconds = 30,
    [switch]$ReadinessOnly,
    [switch]$PrepareOnly,
    [switch]$NoResume
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'NativeHostUpdate.Core.ps1')

function Write-AtomicJsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $partial = "$Path.part"
    ConvertTo-Json -InputObject $Value -Depth 8 | Set-Content -LiteralPath $partial -Encoding UTF8
    Move-Item -LiteralPath $partial -Destination $Path -Force
}

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-AirPlayPcmSnapshot {
    param([Parameter(Mandatory = $true)][string]$StatusPath)

    if (-not (Test-Path -LiteralPath $StatusPath -PathType Leaf)) {
        return [pscustomobject]@{ Exists = $false; PcmFrames = 0L; State = ''; LastWriteUtc = $null }
    }
    $status = Read-JsonFile $StatusPath
    return [pscustomobject]@{
        Exists = $true
        PcmFrames = [long]$status.pcm_frames
        State = [string]$status.state
        LastWriteUtc = (Get-Item -LiteralPath $StatusPath).LastWriteTimeUtc
    }
}

function Assert-AirPlayPcmQuietPeriod {
    param(
        [Parameter(Mandatory = $true)][string]$StatusPath,
        [Parameter(Mandatory = $true)][int]$Seconds
    )

    $before = Get-AirPlayPcmSnapshot $StatusPath
    Start-Sleep -Seconds $Seconds
    $after = Get-AirPlayPcmSnapshot $StatusPath
    if (-not $before.Exists -and $after.Exists) {
        throw 'AirPlay PCM appeared during the update quiet probe.'
    }
    if ($before.Exists -and $after.Exists -and
        -not (Test-AirPlayPcmQuiet -BeforePcmFrames $before.PcmFrames -AfterPcmFrames $after.PcmFrames)) {
        throw "AirPlay PCM is active ($($before.PcmFrames) -> $($after.PcmFrames)). Pause playback before updating."
    }
    return [pscustomobject]@{
        Quiet = $true
        PcmFrames = $(if ($after.Exists) { $after.PcmFrames } else { $before.PcmFrames })
        ObservedSeconds = $Seconds
    }
}

function Get-CurrentCommandSequence {
    param([Parameter(Mandatory = $true)][string]$CommandPath)

    $command = Read-JsonFile $CommandPath
    if ($null -eq $command -or $null -eq $command.sequence) { return 0L }
    return [long]$command.sequence
}

function Write-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$CommandPath,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Command
    )

    $sequence = Get-NativeCommandSequence -CurrentSequence (Get-CurrentCommandSequence $CommandPath)
    $payload = [ordered]@{ sequence = $sequence }
    foreach ($key in $Command.Keys) { $payload[$key] = $Command[$key] }
    Write-AtomicJsonFile -Path $CommandPath -Value $payload
    return $sequence
}

function Test-ProcessAlive {
    param([int]$Id = 0)

    if ($Id -le 0) { return $false }
    return $null -ne (Get-Process -Id $Id -ErrorAction SilentlyContinue)
}

function Stop-KnownProcess {
    param(
        [int]$Id = 0,
        [string]$ExpectedPath = '',
        [switch]$PowerShellOnly
    )

    if ($Id -le 0) { return }
    $process = Get-Process -Id $Id -ErrorAction SilentlyContinue
    if ($null -eq $process) { return }
    if ($PowerShellOnly -and $process.ProcessName -notin @('powershell', 'pwsh')) {
        throw "Refusing to stop unexpected controller process $($process.ProcessName) ($($process.Id))."
    }
    if ($ExpectedPath) {
        $actualPath = [System.IO.Path]::GetFullPath([string]$process.Path)
        $requiredPath = [System.IO.Path]::GetFullPath($ExpectedPath)
        if (-not $actualPath.Equals($requiredPath, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to stop process $($process.Id) at unexpected path $actualPath."
        }
    }
    Stop-Process -Id $process.Id -Force
    try { $process.WaitForExit(5000) | Out-Null } catch { }
}

function Wait-ControllerState {
    param(
        [Parameter(Mandatory = $true)][string]$StatusPath,
        [Parameter(Mandatory = $true)][string[]]$ExpectedStates,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [object]$WrittenAfterUtc = $null
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $StatusPath -PathType Leaf) {
            $item = Get-Item -LiteralPath $StatusPath
            if ($null -eq $WrittenAfterUtc -or $item.LastWriteTimeUtc -ge ([DateTime]$WrittenAfterUtc)) {
                try {
                    $status = Read-JsonFile $StatusPath
                    if ([string]$status.state -eq 'error') {
                        throw "Native controller reported an error: $($status.error)"
                    }
                    if ([string]$status.state -in $ExpectedStates) { return $status }
                } catch {
                    if ($_.Exception.Message -like 'Native controller reported an error:*') { throw }
                }
            }
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out waiting for native controller state: $($ExpectedStates -join ', ')."
}

function Start-NativeController {
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [Parameter(Mandatory = $true)][string]$PidPath,
        [Parameter(Mandatory = $true)][string]$StatusPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    # A recently written error from the previous controller can otherwise be
    # mistaken for the new process response. Status is ephemeral telemetry;
    # remove it before launch and wait for the new controller to recreate it.
    Remove-Item -LiteralPath $StatusPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath "$StatusPath.part" -Force -ErrorAction SilentlyContinue
    $startedUtc = [DateTime]::UtcNow
    $controller = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        (Join-Path $PSScriptRoot 'HostController.ps1'),
        '-Root',
        $RootPath
    ) -WindowStyle Hidden -PassThru
    $controller.Id | Set-Content -LiteralPath $PidPath -Encoding ascii
    Wait-ControllerState -StatusPath $StatusPath -ExpectedStates @('stopped') `
        -TimeoutSeconds $TimeoutSeconds -WrittenAfterUtc $startedUtc | Out-Null
    return $controller
}

function Get-ResumeCommand {
    param($ControllerStatus, $LastCommand, [switch]$Disabled)

    if ($Disabled -or $null -eq $ControllerStatus) { return $null }
    $hostAlive = Test-ProcessAlive $(if ($ControllerStatus.host_pid) { [int]$ControllerStatus.host_pid } else { $null })
    $airplayAlive = Test-ProcessAlive $(if ($ControllerStatus.airplay_pid) { [int]$ControllerStatus.airplay_pid } else { $null })
    if (-not $hostAlive -and -not $airplayAlive) { return $null }

    $profileName = if ($ControllerStatus.profile_name) {
        [string]$ControllerStatus.profile_name
    } elseif ($LastCommand.profile_name) {
        [string]$LastCommand.profile_name
    } else {
        'Six-track realtime'
    }
    $trackCount = if ($ControllerStatus.track_count) {
        [int]$ControllerStatus.track_count
    } elseif ($LastCommand.track_count) {
        [int]$LastCommand.track_count
    } else {
        6
    }
    if ($trackCount -notin @(2, 4, 6)) { throw "Unsupported saved track count: $trackCount" }
    $monitorStem = if ($LastCommand.monitor_stem) { [string]$LastCommand.monitor_stem } else { 'mix' }

    if ([string]$ControllerStatus.input_source -eq 'airplay' -or
        [string]$LastCommand.action -eq 'start_airplay') {
        return [ordered]@{
            action = 'start_airplay'
            monitor_stem = $monitorStem
            profile_name = $profileName
            track_count = $trackCount
            input_source = 'airplay'
        }
    }
    if ([string]$LastCommand.action -eq 'start' -and $LastCommand.process_id) {
        return [ordered]@{
            action = 'start'
            process_id = [int]$LastCommand.process_id
            monitor_stem = $monitorStem
            profile_name = $profileName
            track_count = $trackCount
            input_source = 'process'
        }
    }
    return $null
}

function Confirm-InstalledRuntime {
    param(
        [Parameter(Mandatory = $true)]$Layout,
        [Parameter(Mandatory = $true)][string]$AudioHash,
        [Parameter(Mandatory = $true)][string]$AirPlayHash,
        [Parameter(Mandatory = $true)][string]$AirPlayPackageHash
    )

    $installedAudioHash = (Get-FileHash -LiteralPath $Layout.AudioLive -Algorithm SHA256).Hash.ToUpperInvariant()
    $installedAirPlayExe = Join-Path $Layout.AirPlayLive 'bin\stem-studio-airplay-host.exe'
    $installedAirPlayHash = (Get-FileHash -LiteralPath $installedAirPlayExe -Algorithm SHA256).Hash.ToUpperInvariant()
    $installedPackageHash = Get-DirectoryManifestHash -Root $Layout.AirPlayLive
    if ($installedAudioHash -ne $AudioHash) { throw 'Installed audio host hash verification failed.' }
    if ($installedAirPlayHash -ne $AirPlayHash) { throw 'Installed AirPlay host hash verification failed.' }
    if ($installedPackageHash -ne $AirPlayPackageHash) { throw 'Installed AirPlay package manifest verification failed.' }
}

$rootPath = if ($Root) {
    [System.IO.Path]::GetFullPath($Root)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
}
$audioCandidatePath = if ($AudioCandidate) {
    [System.IO.Path]::GetFullPath($AudioCandidate)
} else {
    Join-Path $rootPath 'data\temp\audio-host-next\stem-studio-audio-host.exe'
}
$airplayCandidatePath = if ($AirPlayCandidateRoot) {
    [System.IO.Path]::GetFullPath($AirPlayCandidateRoot)
} else {
    Join-Path $rootPath 'data\temp\airplay-host-next'
}
$airplayStatusPath = Join-Path $rootPath 'data\live\airplay-status.json'

$readiness = Get-NativeUpdateReadiness `
    -Root $rootPath `
    -AudioCandidate $audioCandidatePath `
    -AirPlayCandidateRoot $airplayCandidatePath `
    -ExpectedAudioHash $ExpectedAudioHash `
    -ExpectedAirPlayHash $ExpectedAirPlayHash
if ($ReadinessOnly) {
    $readiness | ConvertTo-Json -Depth 5
    return
}
if (-not $readiness.Ready -and $readiness.Code -ne 'ActiveStream') {
    throw "Native update preflight failed [$($readiness.Code)]: $($readiness.Message)"
}

$quiet = Assert-AirPlayPcmQuietPeriod -StatusPath $airplayStatusPath -Seconds $QuietProbeSeconds
$transactionId = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$layout = Get-NativeHostInstallLayout -Root $rootPath -TransactionId $transactionId
if (-not (Test-Path -LiteralPath $layout.AudioLive -PathType Leaf)) {
    throw "Installed audio host was not found: $($layout.AudioLive)"
}
if (-not (Test-Path -LiteralPath $layout.AirPlayLive -PathType Container)) {
    throw "Installed AirPlay package was not found: $($layout.AirPlayLive)"
}

$stageAudioDirectory = Join-Path $layout.StageRoot 'audio-host'
$stageAudio = Join-Path $stageAudioDirectory 'stem-studio-audio-host.exe'
$stageAirPlay = Join-Path $layout.StageRoot 'airplay-host'
$stageAirPlayExe = Join-Path $stageAirPlay 'bin\stem-studio-airplay-host.exe'
$backupAudioDirectory = Join-Path $layout.BackupRoot 'audio-host'
$backupAudio = Join-Path $backupAudioDirectory 'stem-studio-audio-host.exe'
$backupAirPlay = Join-Path $layout.BackupRoot 'airplay-host'

New-Item -ItemType Directory -Force -Path $stageAudioDirectory, $backupAudioDirectory | Out-Null
$sourcePackageHash = Get-DirectoryManifestHash -Root $airplayCandidatePath
Copy-Item -LiteralPath $audioCandidatePath -Destination $stageAudio
Copy-Item -LiteralPath $airplayCandidatePath -Destination $stageAirPlay -Recurse

$stagedAudioHash = (Get-FileHash -LiteralPath $stageAudio -Algorithm SHA256).Hash.ToUpperInvariant()
$stagedAirPlayHash = (Get-FileHash -LiteralPath $stageAirPlayExe -Algorithm SHA256).Hash.ToUpperInvariant()
$stagedPackageHash = Get-DirectoryManifestHash -Root $stageAirPlay
if ($stagedAudioHash -ne $readiness.AudioHash) { throw 'Staged audio host hash verification failed.' }
if ($stagedAirPlayHash -ne $readiness.AirPlayHash) { throw 'Staged AirPlay host hash verification failed.' }
if ($stagedPackageHash -ne $sourcePackageHash) { throw 'Staged AirPlay package manifest verification failed.' }
if ((Get-DirectoryManifestHash -Root $airplayCandidatePath) -ne $sourcePackageHash) {
    throw 'AirPlay candidate changed while it was being staged.'
}

$state = [ordered]@{
    transaction_id = $transactionId
    state = 'prepared'
    prepared_at = [DateTime]::UtcNow.ToString('o')
    root = $rootPath
    transaction_root = $layout.TransactionRoot
    audio_hash = $stagedAudioHash
    airplay_hash = $stagedAirPlayHash
    airplay_package_hash = $stagedPackageHash
    quiet_pcm_frames = $quiet.PcmFrames
    docker_restarted = $false
    rollback_available = $false
}
Write-AtomicJsonFile -Path $layout.StatePath -Value $state
if ($PrepareOnly) {
    $state | ConvertTo-Json -Depth 5
    return
}

# Recheck immediately before stopping the live native processes.
Assert-AirPlayPcmQuietPeriod -StatusPath $airplayStatusPath `
    -Seconds ([Math]::Min(2, $QuietProbeSeconds)) | Out-Null

$previousStatus = Read-JsonFile $layout.ControllerStatusPath
$previousCommand = Read-JsonFile $layout.CommandPath
$resumeCommand = Get-ResumeCommand -ControllerStatus $previousStatus -LastCommand $previousCommand -Disabled:$NoResume
$hostPid = if ($previousStatus.host_pid) { [int]$previousStatus.host_pid } else { 0 }
$airplayPid = if ($previousStatus.airplay_pid) { [int]$previousStatus.airplay_pid } else { 0 }
$controllerPid = if (Test-Path -LiteralPath $layout.ControllerPidPath -PathType Leaf) {
    [int](Get-Content -LiteralPath $layout.ControllerPidPath -Raw)
} else {
    0
}
$hadController = Test-ProcessAlive $controllerPid

$audioMoved = $false
$airplayMoved = $false
$newAudioInstalled = $false
$newAirPlayInstalled = $false
$newController = $null
$oldProcessesStopped = $false

try {
    if ($hadController) {
        Write-NativeCommand -CommandPath $layout.CommandPath -Command ([ordered]@{ action = 'stop' }) | Out-Null
        try {
            Wait-ControllerState -StatusPath $layout.ControllerStatusPath -ExpectedStates @('stopped') `
                -TimeoutSeconds $NativeStopTimeoutSeconds | Out-Null
        } catch {
            # Force the exact recorded native processes below if the controller did not respond.
        }
    }
    Stop-KnownProcess -Id $hostPid -ExpectedPath $layout.AudioLive
    Stop-KnownProcess -Id $airplayPid -ExpectedPath (Join-Path $layout.AirPlayLive 'bin\stem-studio-airplay-host.exe')
    Stop-KnownProcess -Id $controllerPid -PowerShellOnly
    Remove-Item -LiteralPath $layout.ControllerPidPath -Force -ErrorAction SilentlyContinue
    $oldProcessesStopped = $true

    Move-Item -LiteralPath $layout.AudioLive -Destination $backupAudio
    $audioMoved = $true
    Move-Item -LiteralPath $layout.AirPlayLive -Destination $backupAirPlay
    $airplayMoved = $true
    Move-Item -LiteralPath $stageAudio -Destination $layout.AudioLive
    $newAudioInstalled = $true
    Move-Item -LiteralPath $stageAirPlay -Destination $layout.AirPlayLive
    $newAirPlayInstalled = $true
    $state.state = 'swapped'
    $state.rollback_available = $true
    Write-AtomicJsonFile -Path $layout.StatePath -Value $state

    Confirm-InstalledRuntime -Layout $layout -AudioHash $stagedAudioHash `
        -AirPlayHash $stagedAirPlayHash -AirPlayPackageHash $stagedPackageHash
    if ($hadController) {
        $newController = Start-NativeController -RootPath $rootPath `
            -PidPath $layout.ControllerPidPath `
            -StatusPath $layout.ControllerStatusPath `
            -TimeoutSeconds $ControllerTimeoutSeconds
        if ($null -ne $resumeCommand) {
            Write-NativeCommand -CommandPath $layout.CommandPath -Command $resumeCommand | Out-Null
            $expectedState = if ($resumeCommand.action -eq 'start_airplay') { 'airplay_waiting' } else { 'capturing' }
            $runtimeStatus = Wait-ControllerState -StatusPath $layout.ControllerStatusPath `
                -ExpectedStates @($expectedState) -TimeoutSeconds $ControllerTimeoutSeconds
            if ([string]$runtimeStatus.playback_priority -ne 'AboveNormal') {
                throw 'The updated playback host did not start at AboveNormal priority.'
            }
            if ($resumeCommand.action -eq 'start_airplay' -and
                [string]$runtimeStatus.airplay_priority -ne 'AboveNormal') {
                throw 'The updated AirPlay host did not start at AboveNormal priority.'
            }
            if (-not (Test-ProcessAlive ([int]$runtimeStatus.host_pid))) {
                throw 'The updated playback host exited during verification.'
            }
            if ($resumeCommand.action -eq 'start_airplay' -and
                -not (Test-ProcessAlive ([int]$runtimeStatus.airplay_pid))) {
                throw 'The updated AirPlay host exited during verification.'
            }
        }
    }

    $state.state = 'installed'
    $state.installed_at = [DateTime]::UtcNow.ToString('o')
    $state.rollback_available = $true
    $state.controller_restarted = $hadController
    $state.resume_action = $(if ($null -ne $resumeCommand) { $resumeCommand.action } else { $null })
    Write-AtomicJsonFile -Path $layout.StatePath -Value $state
    $state | ConvertTo-Json -Depth 5
} catch {
    $failure = $_.Exception.Message
    $rollbackError = $null
    if ($oldProcessesStopped) {
        try {
            $currentStatus = Read-JsonFile $layout.ControllerStatusPath
            if ($null -ne $currentStatus) {
                Stop-KnownProcess -Id $(if ($currentStatus.host_pid) { [int]$currentStatus.host_pid } else { 0 }) `
                    -ExpectedPath $layout.AudioLive
                Stop-KnownProcess -Id $(if ($currentStatus.airplay_pid) { [int]$currentStatus.airplay_pid } else { 0 }) `
                    -ExpectedPath (Join-Path $layout.AirPlayLive 'bin\stem-studio-airplay-host.exe')
            }
            $currentControllerPid = if (Test-Path -LiteralPath $layout.ControllerPidPath -PathType Leaf) {
                [int](Get-Content -LiteralPath $layout.ControllerPidPath -Raw)
            } else {
                0
            }
            Stop-KnownProcess -Id $currentControllerPid -PowerShellOnly
            Remove-Item -LiteralPath $layout.ControllerPidPath -Force -ErrorAction SilentlyContinue

            $failedRoot = Join-Path $layout.TransactionRoot 'failed-install'
            $failedAudioDirectory = Join-Path $failedRoot 'audio-host'
            $failedAirPlay = Join-Path $failedRoot 'airplay-host'
            New-Item -ItemType Directory -Force -Path $failedAudioDirectory | Out-Null
            if ($newAudioInstalled -and (Test-Path -LiteralPath $layout.AudioLive -PathType Leaf)) {
                Move-Item -LiteralPath $layout.AudioLive `
                    -Destination (Join-Path $failedAudioDirectory 'stem-studio-audio-host.exe')
            }
            if ($newAirPlayInstalled -and (Test-Path -LiteralPath $layout.AirPlayLive -PathType Container)) {
                Move-Item -LiteralPath $layout.AirPlayLive -Destination $failedAirPlay
            }
            if ($audioMoved -and (Test-Path -LiteralPath $backupAudio -PathType Leaf)) {
                Move-Item -LiteralPath $backupAudio -Destination $layout.AudioLive
            }
            if ($airplayMoved -and (Test-Path -LiteralPath $backupAirPlay -PathType Container)) {
                Move-Item -LiteralPath $backupAirPlay -Destination $layout.AirPlayLive
            }
            if ($hadController) {
                $newController = Start-NativeController -RootPath $rootPath `
                    -PidPath $layout.ControllerPidPath `
                    -StatusPath $layout.ControllerStatusPath `
                    -TimeoutSeconds $ControllerTimeoutSeconds
                if ($null -ne $resumeCommand) {
                    Write-NativeCommand -CommandPath $layout.CommandPath -Command $resumeCommand | Out-Null
                }
            }
        } catch {
            $rollbackError = $_.Exception.Message
        }
    }
    $state.state = $(if ($rollbackError) { 'rollback_failed' } elseif ($oldProcessesStopped) { 'rolled_back' } else { 'failed_before_stop' })
    $state.failed_at = [DateTime]::UtcNow.ToString('o')
    $state.error = $failure
    $state.rollback_error = $rollbackError
    try { Write-AtomicJsonFile -Path $layout.StatePath -Value $state } catch { }
    if ($rollbackError) {
        throw "Native update failed: $failure Rollback also failed: $rollbackError"
    }
    throw "Native update failed and was rolled back: $failure"
}
