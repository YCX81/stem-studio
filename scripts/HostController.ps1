param([Parameter(Mandatory = $true)][string]$Root)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'HostController.Core.ps1')
# Windows PowerShell Start-Process rejects environment blocks that contain both
# PATH and Path (some launchers create this duplicate). Normalize it once here.
$processPath = $env:PATH
[Environment]::SetEnvironmentVariable('PATH', $null, 'Process')
[Environment]::SetEnvironmentVariable('Path', $processPath, 'Process')
$rootPath = [System.IO.Path]::GetFullPath($Root)
$live = Join-Path $rootPath 'data\live'
$hostExe = Join-Path $rootPath 'host\bin\stem-studio-audio-host.exe'
$airplayBin = Join-Path $rootPath 'airplay-host\bin'
$airplayExe = Join-Path $airplayBin 'stem-studio-airplay-host.exe'
$gstPlugins = 'airplay-host\lib\gstreamer-1.0'
$gstScanner = 'airplay-host\libexec\gstreamer-1.0\gst-plugin-scanner.exe'
$lastCommand = 0L
$captureProcess = $null
$airplayProcess = $null
$playbackPriority = $null
$airplayPriority = $null
$activeProfileName = ''
$activeDeviceEndpoint = ''
$activeHopSeconds = 0
$activeLanInterface = $null
$audioScanCountdown = 0
$controllerHeartbeat = Join-Path $live 'controller-heartbeat.json'
$musicWatchPath = Join-Path $live 'music-watch.json'
$musicWatch = $null
$autoRestartNotBefore = [DateTimeOffset]::MinValue

function Write-AtomicJson([string]$Path, $Value) {
    $partial = "$Path.part"
    ConvertTo-Json -InputObject $Value -Depth 4 | Set-Content -LiteralPath $partial -Encoding UTF8
    Move-Item -LiteralPath $partial -Destination $Path -Force
}

function Stop-AudioHostProcess($Process) {
    if ($null -eq $Process -or $Process.HasExited) { return }
    try {
        $eventName = "Local\StemStudioAudioHostStop-$($Process.Id)"
        $stopEvent = [System.Threading.EventWaitHandle]::OpenExisting($eventName)
        try { $stopEvent.Set() | Out-Null } finally { $stopEvent.Dispose() }
    } catch { }
    if (-not $Process.WaitForExit(5000)) {
        Stop-Process -Id $Process.Id -Force
        $Process.WaitForExit(3000) | Out-Null
    }
}

function Stop-Playback {
    if ($null -ne $script:captureProcess -and -not $script:captureProcess.HasExited) {
        Stop-AudioHostProcess $script:captureProcess
    }
    $script:captureProcess = $null
    $script:playbackPriority = $null
    $script:activeProfileName = ''
    $script:activeDeviceEndpoint = ''
    $script:activeHopSeconds = 0
}

function Stop-AirPlayReceiver {
    if ($null -ne $script:airplayProcess -and -not $script:airplayProcess.HasExited) {
        Stop-Process -Id $script:airplayProcess.Id -Force
        $script:airplayProcess.WaitForExit(3000) | Out-Null
    }
    $script:airplayProcess = $null
    $script:airplayPriority = $null
    $script:activeLanInterface = $null
    Remove-Item -LiteralPath (Join-Path $live 'airplay-status.json') -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $live 'airplay-status.json.part') -Force -ErrorAction SilentlyContinue
}

function Stop-Capture {
    Stop-Playback
    Stop-AirPlayReceiver
}

function New-CommandSequence {
    $candidate = [long]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() * 1000000L)
    return [Math]::Max($candidate, ($script:lastCommand + 1L))
}

function Write-AutomaticCommand([string]$Action, [string]$Reason, $Watch, [Nullable[int]]$ProcessId = $null) {
    $payload = [ordered]@{
        sequence = New-CommandSequence
        action = $Action
        reason = $Reason
        automatic = $true
    }
    if ($null -ne $ProcessId) {
        $payload.process_id = [int]$ProcessId
    }
    if ($Action -eq 'start') {
        $payload.monitor_stem = $Watch.monitor_stem
        $payload.profile_name = $Watch.profile_name
        $payload.track_count = $Watch.track_count
        $payload.hop_seconds = $Watch.hop_seconds
        if ($Watch.device_endpoint) {
            $payload.device_endpoint = $Watch.device_endpoint
        }
    }
    Write-AtomicJson (Join-Path $live 'command.json') $payload
}

function Write-MusicWatchStatus([string]$State, [string]$Reason = '') {
    $status = [ordered]@{
        state = $State
        input_scope = 'process'
        auto_watch = $true
        watch_scope = 'supported_music_players'
        last_process_name = $script:musicWatch.process_name
        last_player_name = $script:musicWatch.player_name
        profile_name = $script:musicWatch.profile_name
        track_count = $script:musicWatch.track_count
        hop_seconds = $script:musicWatch.hop_seconds
    }
    if ($Reason) { $status.reason = $Reason }
    Write-AtomicJson (Join-Path $live 'controller-status.json') $status
}

function Save-MusicWatch {
    if ($null -eq $script:musicWatch) {
        Remove-Item -LiteralPath $musicWatchPath -Force -ErrorAction SilentlyContinue
        return
    }
    Write-AtomicJson $musicWatchPath $script:musicWatch
}

function Clear-LivePipelineDirectory([string]$Name) {
    $directory = [System.IO.Path]::GetFullPath((Join-Path $live $Name))
    $liveRoot = [System.IO.Path]::GetFullPath($live).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar
    )
    if (-not $directory.StartsWith(
            $liveRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clear a live directory outside the project: $directory"
    }
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        $children = @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)
        if ($children.Count -eq 0) { return }
        foreach ($child in $children) {
            Remove-Item -LiteralPath $child.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 100
    }
    $remaining = @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)
    if ($remaining.Count -gt 0) {
        throw "Cannot reset live pipeline directory: $directory"
    }
}

function Reset-LivePipeline([long]$CommandSequence) {
    $markerPath = Join-Path $live 'capture-session.json'
    Write-AtomicJson $markerPath ([ordered]@{
        state = 'resetting'
        command_sequence = $CommandSequence
        reset_at = [DateTime]::UtcNow.ToString('o')
    })
    Start-Sleep -Milliseconds 300
    foreach ($name in @('inbox', 'outbox', 'work', 'failed')) {
        Clear-LivePipelineDirectory $name
    }
    Write-AtomicJson $markerPath ([ordered]@{
        state = 'ready'
        command_sequence = $CommandSequence
        initial_sequence = 1
        reset_at = [DateTime]::UtcNow.ToString('o')
    })
}

function Update-AudioRouting {
    try {
        $devices = @(Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction Stop | ForEach-Object { [string]$_.FriendlyName })
        $virtual = @($devices | Where-Object { $_ -match '(?i)(VB-Audio|CABLE Input|VoiceMeeter|Virtual Audio|Scream)' })
        Write-AtomicJson (Join-Path $live 'audio-routing.json') ([ordered]@{
            virtual_device_found = ($virtual.Count -gt 0)
            virtual_devices = $virtual
            devices = $devices
            checked_at = [DateTime]::UtcNow.ToString('o')
        })
    } catch {
        Write-AtomicJson (Join-Path $live 'audio-routing.json') ([ordered]@{
            virtual_device_found = $false
            virtual_devices = @()
            error = $_.Exception.Message
            checked_at = [DateTime]::UtcNow.ToString('o')
        })
    }
}

function Get-AirPlayLanInterface {
    try {
        $adapters = @(Get-NetAdapter -IncludeHidden -ErrorAction Stop | Where-Object {
            $_.HardwareInterface -and $_.Status -eq 'Up'
        })
        $interfaces = @(Get-NetIPInterface -AddressFamily IPv4 -ErrorAction Stop | Where-Object {
            $_.ConnectionState -eq 'Connected'
        })
        $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object {
            $_.IPAddress -notmatch '^(127\.|169\.254\.|0\.)'
        })

        $candidates = foreach ($adapter in $adapters) {
            $interface = $interfaces | Where-Object InterfaceIndex -eq $adapter.InterfaceIndex | Select-Object -First 1
            if ($null -eq $interface) { continue }
            foreach ($address in ($addresses | Where-Object InterfaceIndex -eq $adapter.InterfaceIndex)) {
                [pscustomobject]@{
                    IPv4 = [string]$address.IPAddress
                    Name = [string]$adapter.Name
                    Mac = ([string]$adapter.MacAddress).Replace('-', ':').ToLowerInvariant()
                    Metric = [int]$interface.InterfaceMetric
                }
            }
        }

        return $candidates | Sort-Object Metric, Name | Select-Object -First 1
    } catch {
        return $null
    }
}

New-Item -ItemType Directory -Force -Path $live | Out-Null
if (-not (Test-Path -LiteralPath $hostExe)) { throw "缺少原生音频宿主：$hostExe" }
$env:Path = "airplay-host\bin;$env:Path"
$env:GST_PLUGIN_SYSTEM_PATH_1_0 = $gstPlugins
$env:GST_PLUGIN_PATH_1_0 = ''
$env:GST_PLUGIN_SCANNER = $gstScanner
$env:GST_PLUGIN_SCANNER_1_0 = $gstScanner
$env:GST_REGISTRY_1_0 = 'data\live\gstreamer-registry.bin'
$env:GST_REGISTRY_FORK = 'no'
$existingCommandPath = Join-Path $live 'command.json'
if (Test-Path -LiteralPath $existingCommandPath) {
    try {
        $existingCommand = Get-Content -LiteralPath $existingCommandPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $lastCommand = [long]$existingCommand.sequence
    } catch {
        $lastCommand = 0L
    }
}
if (Test-Path -LiteralPath $musicWatchPath) {
    try {
        $savedWatch = Get-Content -LiteralPath $musicWatchPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $supportedPlayer = Get-SupportedMusicPlayerName ([string]$savedWatch.process_name)
        if ($null -ne $supportedPlayer) {
            $musicWatch = [ordered]@{
                process_name = $supportedPlayer.ProcessName
                player_name = $supportedPlayer.DisplayName
                process_id = [int]$savedWatch.process_id
                monitor_stem = [string]$savedWatch.monitor_stem
                profile_name = [string]$savedWatch.profile_name
                track_count = Assert-LiveTrackCount ([int]$savedWatch.track_count)
                hop_seconds = Assert-LiveHopSeconds ([int]$savedWatch.hop_seconds)
                device_endpoint = Assert-DeviceAudioEndpoint ([string]$savedWatch.device_endpoint)
            }
        }
    } catch {
        $musicWatch = $null
        Remove-Item -LiteralPath $musicWatchPath -Force -ErrorAction SilentlyContinue
    }
}
if ($null -eq $musicWatch -and $null -ne $existingCommand -and [string]$existingCommand.action -eq 'start') {
    try {
        $previousProcessId = [int]$existingCommand.process_id
        $processSnapshot = Get-Content -LiteralPath (Join-Path $live 'processes.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $previousProcess = $processSnapshot | Where-Object { [int]$_.pid -eq $previousProcessId } | Select-Object -First 1
        $supportedPlayer = Get-SupportedMusicPlayerName ([string]$previousProcess.name)
        if ($null -ne $supportedPlayer) {
            $musicWatch = [ordered]@{
                process_name = $supportedPlayer.ProcessName
                player_name = $supportedPlayer.DisplayName
                process_id = $previousProcessId
                monitor_stem = [string]$existingCommand.monitor_stem
                profile_name = [string]$existingCommand.profile_name
                track_count = Assert-LiveTrackCount ([int]$existingCommand.track_count)
                hop_seconds = Assert-LiveHopSeconds ([int]$existingCommand.hop_seconds)
                device_endpoint = $(if ($null -ne $existingCommand.PSObject.Properties['device_endpoint']) { Assert-DeviceAudioEndpoint ([string]$existingCommand.device_endpoint) } else { '' })
            }
            Save-MusicWatch
        }
    } catch {
        $musicWatch = $null
    }
}
if ($null -ne $musicWatch) {
    Write-MusicWatchStatus -State 'waiting_for_player' -Reason 'controller_started'
    Write-AutomaticCommand -Action 'stop' -Reason 'controller_waiting_for_music_player' -Watch $musicWatch
} else {
    Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='stopped' })
}

try {
    while ($true) {
        Write-AtomicJson $controllerHeartbeat (New-ControllerHeartbeat -ProcessId $PID)
        if ($audioScanCountdown -le 0) {
            Update-AudioRouting
            $audioScanCountdown = 5
        }
        $audioScanCountdown--
        $sessionId = (Get-Process -Id $PID).SessionId
        $processes = @(Get-Process | Where-Object {
            $_.Id -ne $PID -and $_.SessionId -eq $sessionId -and $null -ne $_.Path
        } | ForEach-Object {
            $description = try { $_.FileVersionInfo.FileDescription } catch { '' }
            [ordered]@{ pid = $_.Id; name = "$($_.ProcessName).exe"; title = $(if ($_.MainWindowTitle) { $_.MainWindowTitle } else { $description }) }
        } | Sort-Object name, pid)
        Write-AtomicJson (Join-Path $live 'processes.json') $processes

        $commandPath = Join-Path $live 'command.json'
        if (Test-Path -LiteralPath $commandPath) {
            try {
                $command = Get-Content -LiteralPath $commandPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ([long]$command.sequence -gt $lastCommand) {
                    $lastCommand = [long]$command.sequence
                    if ($command.action -eq 'open_audio_settings') {
                        Start-Process 'ms-settings:apps-volume'
                        Write-AtomicJson (Join-Path $live 'routing-action.json') ([ordered]@{ state='opened'; opened_at=[DateTime]::UtcNow.ToString('o') })
                    } elseif ($command.action -eq 'enable_music_watch') {
                        Stop-Capture
                        $profileName = [string]$command.profile_name
                        $trackCount = Assert-LiveTrackCount ([int]$command.track_count)
                        $deviceEndpoint = ''
                        if ($null -ne $command.PSObject.Properties['device_endpoint']) {
                            $deviceEndpoint = Assert-DeviceAudioEndpoint ([string]$command.device_endpoint)
                        }
                        $hopSeconds = 6
                        if ($null -ne $command.PSObject.Properties['hop_seconds']) {
                            $hopSeconds = Assert-LiveHopSeconds ([int]$command.hop_seconds)
                        }
                        $musicWatch = [ordered]@{
                            process_name = ''
                            player_name = ''
                            process_id = 0
                            monitor_stem = [string]$command.monitor_stem
                            profile_name = $profileName
                            track_count = $trackCount
                            hop_seconds = $hopSeconds
                            device_endpoint = $deviceEndpoint
                        }
                        Save-MusicWatch
                        $autoRestartNotBefore = [DateTimeOffset]::UtcNow
                        Write-MusicWatchStatus -State 'waiting_for_player' -Reason 'music_monitor_enabled'
                        Write-AutomaticCommand -Action 'stop' -Reason 'music_monitor_waiting_for_player' -Watch $musicWatch
                    } elseif ($command.action -eq 'start_airplay') {
                        $musicWatch = $null
                        Save-MusicWatch
                        if (-not (Test-Path -LiteralPath $airplayExe)) { throw "缺少内置 AirPlay 宿主：$airplayExe" }
                        $profileName = [string]$command.profile_name
                        $trackCount = Assert-LiveTrackCount ([int]$command.track_count)
                        $deviceEndpoint = ''
                        if ($null -ne $command.PSObject.Properties['device_endpoint']) {
                            $deviceEndpoint = Assert-DeviceAudioEndpoint ([string]$command.device_endpoint)
                        }
                        $hopSeconds = 6
                        if ($null -ne $command.PSObject.Properties['hop_seconds']) {
                            $hopSeconds = Assert-LiveHopSeconds ([int]$command.hop_seconds)
                        }
                        $airplayRunning = $null -ne $airplayProcess -and -not $airplayProcess.HasExited
                        $playbackRunning = $null -ne $captureProcess -and -not $captureProcess.HasExited
                        $startPlan = Get-AirPlayStartPlan -AirPlayRunning $airplayRunning -PlaybackRunning $playbackRunning -CurrentProfile $activeProfileName -RequestedProfile $profileName -CurrentDeviceEndpoint $activeDeviceEndpoint -RequestedDeviceEndpoint $deviceEndpoint -CurrentHopSeconds $activeHopSeconds -RequestedHopSeconds $hopSeconds
                        if ($startPlan.RestartAirPlay) {
                            Stop-Capture
                        } elseif ($startPlan.RestartPlayback) {
                            Stop-Playback
                        }
                        $stdout = Join-Path $live 'playback-stdout.log'
                        $stderr = Join-Path $live 'playback-stderr.log'
                        if ($startPlan.RestartPlayback) {
                            $playbackArgs = @(New-AudioHostArgumentList -Source '--playback-only' -LiveDirectory 'data\live' -TrackCount $trackCount -DeviceEndpoint $deviceEndpoint -HopSeconds $hopSeconds)
                            $captureProcess = Start-Process -FilePath $hostExe -ArgumentList $playbackArgs -WorkingDirectory $rootPath -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
                            $playbackPriority = Set-StreamingProcessPriority $captureProcess
                            $activeProfileName = $profileName
                            $activeDeviceEndpoint = $deviceEndpoint
                            $activeHopSeconds = $hopSeconds
                        }
                        $airplayStdout = Join-Path $live 'airplay-stdout.log'
                        $airplayStderr = Join-Path $live 'airplay-stderr.log'
                        if ($startPlan.RestartAirPlay) {
                            $activeLanInterface = Get-AirPlayLanInterface
                            $airplayArgs = @(
                                '-n', 'StemStudio',
                                '-nh',
                                '-vs', '0',
                                '-stem-live-dir', 'data/live',
                                '-stem-hop-seconds', [string]$hopSeconds
                            )
                            if ($null -ne $activeLanInterface) {
                                $env:STEM_STUDIO_MDNS_IPV4 = $activeLanInterface.IPv4
                                if ($activeLanInterface.Mac) { $airplayArgs += @('-m', $activeLanInterface.Mac) }
                            } else {
                                Remove-Item Env:STEM_STUDIO_MDNS_IPV4 -ErrorAction SilentlyContinue
                            }
                            try {
                                $airplayProcess = Start-Process -FilePath $airplayExe -ArgumentList $airplayArgs -WorkingDirectory $rootPath -WindowStyle Hidden -RedirectStandardOutput $airplayStdout -RedirectStandardError $airplayStderr -PassThru
                                $airplayPriority = Set-StreamingProcessPriority $airplayProcess
                            } catch {
                                Stop-Capture
                                throw
                            }
                        }
                        Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='airplay_waiting'; input_source='airplay'; monitor_stem='mix'; profile_name=$profileName; track_count=$trackCount; hop_seconds=$hopSeconds; device_endpoint=$(if ($deviceEndpoint) { $deviceEndpoint } else { $null }); host_pid=$captureProcess.Id; airplay_pid=$airplayProcess.Id; playback_priority=$playbackPriority; airplay_priority=$airplayPriority; lan_interface=$(if ($null -ne $activeLanInterface) { $activeLanInterface.Name } else { $null }); lan_ipv4=$(if ($null -ne $activeLanInterface) { $activeLanInterface.IPv4 } else { $null }); connection_reused=(-not $startPlan.RestartAirPlay) })
                    } elseif ($command.action -eq 'start') {
                        Stop-Capture
                        $automaticStart = $null -ne $command.PSObject.Properties['automatic'] -and [bool]$command.automatic
                        if (-not $automaticStart) {
                            $musicWatch = $null
                        }
                        $requestedProcessId = [int]$command.process_id
                        if ($requestedProcessId -le 0) {
                            throw 'System-wide capture is disabled. Select a supported music player.'
                        }
                        $target = Get-Process -Id $requestedProcessId -ErrorAction Stop
                        $supportedPlayer = Get-SupportedMusicPlayerName $target.ProcessName
                        if ($null -eq $supportedPlayer) {
                            throw "Unsupported capture target: $($target.ProcessName). Select a music player."
                        }
                        $captureTarget = Resolve-MusicCaptureProcess -PlayerProcess $target -Processes @(Get-Process)
                        $captureSource = [string]$captureTarget.Id
                        $targetProcessName = $target.ProcessName
                        $stdout = Join-Path $live 'capture-stdout.log'
                        $stderr = Join-Path $live 'capture-stderr.log'
                        $profileName = [string]$command.profile_name
                        $trackCount = Assert-LiveTrackCount ([int]$command.track_count)
                        $deviceEndpoint = ''
                        if ($null -ne $command.PSObject.Properties['device_endpoint']) {
                            $deviceEndpoint = Assert-DeviceAudioEndpoint ([string]$command.device_endpoint)
                        }
                        $hopSeconds = 6
                        if ($null -ne $command.PSObject.Properties['hop_seconds']) {
                            $hopSeconds = Assert-LiveHopSeconds ([int]$command.hop_seconds)
                        }
                        Reset-LivePipeline ([long]$command.sequence)
                        $captureArgs = @(New-AudioHostArgumentList -Source $captureSource -LiveDirectory 'data\live' -TrackCount $trackCount -DeviceEndpoint $deviceEndpoint -HopSeconds $hopSeconds)
                        $captureProcess = Start-Process -FilePath $hostExe -ArgumentList $captureArgs -WorkingDirectory $rootPath -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
                        $playbackPriority = Set-StreamingProcessPriority $captureProcess
                        $activeProfileName = $profileName
                        $activeDeviceEndpoint = $deviceEndpoint
                        $activeHopSeconds = $hopSeconds
                        $musicWatch = [ordered]@{
                            process_name = $supportedPlayer.ProcessName
                            player_name = $supportedPlayer.DisplayName
                            process_id = $requestedProcessId
                            monitor_stem = [string]$command.monitor_stem
                            profile_name = $profileName
                            track_count = $trackCount
                            hop_seconds = $hopSeconds
                            device_endpoint = $deviceEndpoint
                        }
                        Save-MusicWatch
                        $autoRestartNotBefore = [DateTimeOffset]::UtcNow.AddSeconds(5)
                        Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='capturing'; process_id=$requestedProcessId; capture_process_id=$captureTarget.Id; process_name=$targetProcessName; capture_process_name=$captureTarget.ProcessName; player_name=$supportedPlayer.DisplayName; input_scope='process'; auto_watch=$true; watch_scope='supported_music_players'; monitor_stem='mix'; profile_name=$profileName; track_count=$trackCount; hop_seconds=$hopSeconds; device_endpoint=$(if ($deviceEndpoint) { $deviceEndpoint } else { $null }); host_pid=$captureProcess.Id; playback_priority=$playbackPriority })
                    } elseif ($command.action -eq 'stop') {
                        Stop-Capture
                        $automaticStop = $null -ne $command.PSObject.Properties['automatic'] -and [bool]$command.automatic
                        if ($automaticStop -and $null -ne $musicWatch) {
                            Write-MusicWatchStatus -State 'waiting_for_player' -Reason ([string]$command.reason)
                        } else {
                            $musicWatch = $null
                            Save-MusicWatch
                            Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='stopped' })
                        }
                    } else {
                        throw "未知控制命令：$($command.action)"
                    }
                }
            } catch {
                Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='error'; error=$_.Exception.Message })
            }
        }
        if ($null -ne $captureProcess -and $captureProcess.HasExited) {
            $exitCode = $captureProcess.ExitCode
            Stop-Playback
            if ($null -ne $musicWatch) {
                $autoRestartNotBefore = [DateTimeOffset]::UtcNow.AddSeconds(10)
                Write-MusicWatchStatus -State 'waiting_for_player' -Reason "audio_host_exited_$exitCode"
            } else {
                Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='error'; error="音频播放宿主已退出，退出码 $exitCode；AirPlay 接收器保持运行"; airplay_pid=$(if ($null -ne $airplayProcess -and -not $airplayProcess.HasExited) { $airplayProcess.Id } else { $null }) })
            }
        }
        if ($null -ne $airplayProcess -and $airplayProcess.HasExited) {
            $exitCode = $airplayProcess.ExitCode
            Stop-Capture
            Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='error'; error="AirPlay 宿主已退出，退出码 $exitCode" })
        }
        if ($null -ne $musicWatch) {
            $watchedProcess = Get-Process -Id ([int]$musicWatch.process_id) -ErrorAction SilentlyContinue
            $captureRunning = $null -ne $captureProcess -and -not $captureProcess.HasExited
            if ($captureRunning -and $null -eq $watchedProcess) {
                Write-AutomaticCommand -Action 'stop' -Reason 'music_player_exited' -Watch $musicWatch
                $autoRestartNotBefore = [DateTimeOffset]::UtcNow.AddSeconds(3)
            } elseif (-not $captureRunning -and [DateTimeOffset]::UtcNow -ge $autoRestartNotBefore) {
                $replacement = Select-MusicPlayerProcess -PreferredProcessName $musicWatch.process_name -Processes @(Get-Process)
                if ($null -ne $replacement) {
                    $musicWatch.process_id = [int]$replacement.Id
                    Write-AutomaticCommand -Action 'start' -Reason 'music_player_started' -Watch $musicWatch -ProcessId $replacement.Id
                    $autoRestartNotBefore = [DateTimeOffset]::UtcNow.AddSeconds(10)
                }
            }
        }
        Start-Sleep -Seconds 2
    }
} finally {
    Remove-Item -LiteralPath $controllerHeartbeat -Force -ErrorAction SilentlyContinue
    Stop-Capture
}
