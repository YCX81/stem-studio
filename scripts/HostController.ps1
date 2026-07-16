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
$activeLanInterface = $null
$audioScanCountdown = 0
$controllerHeartbeat = Join-Path $live 'controller-heartbeat.json'

function Write-AtomicJson([string]$Path, $Value) {
    $partial = "$Path.part"
    ConvertTo-Json -InputObject $Value -Depth 4 | Set-Content -LiteralPath $partial -Encoding UTF8
    Move-Item -LiteralPath $partial -Destination $Path -Force
}

function Stop-Playback {
    if ($null -ne $script:captureProcess -and -not $script:captureProcess.HasExited) {
        Stop-Process -Id $script:captureProcess.Id -Force
        $script:captureProcess.WaitForExit(3000) | Out-Null
    }
    $script:captureProcess = $null
    $script:playbackPriority = $null
    $script:activeProfileName = ''
    $script:activeDeviceEndpoint = ''
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
Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='stopped' })

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
                    } elseif ($command.action -eq 'start_airplay') {
                        if (-not (Test-Path -LiteralPath $airplayExe)) { throw "缺少内置 AirPlay 宿主：$airplayExe" }
                        $profileName = [string]$command.profile_name
                        $trackCount = Assert-LiveTrackCount ([int]$command.track_count)
                        $deviceEndpoint = ''
                        if ($null -ne $command.PSObject.Properties['device_endpoint']) {
                            $deviceEndpoint = Assert-DeviceAudioEndpoint ([string]$command.device_endpoint)
                        }
                        $airplayRunning = $null -ne $airplayProcess -and -not $airplayProcess.HasExited
                        $playbackRunning = $null -ne $captureProcess -and -not $captureProcess.HasExited
                        $startPlan = Get-AirPlayStartPlan -AirPlayRunning $airplayRunning -PlaybackRunning $playbackRunning -CurrentProfile $activeProfileName -RequestedProfile $profileName -CurrentDeviceEndpoint $activeDeviceEndpoint -RequestedDeviceEndpoint $deviceEndpoint
                        if ($startPlan.RestartAirPlay) {
                            Stop-Capture
                        } elseif ($startPlan.RestartPlayback) {
                            Stop-Playback
                        }
                        $stdout = Join-Path $live 'playback-stdout.log'
                        $stderr = Join-Path $live 'playback-stderr.log'
                        if ($startPlan.RestartPlayback) {
                            $playbackArgs = @(New-AudioHostArgumentList -Source '--playback-only' -LiveDirectory 'data\live' -TrackCount $trackCount -DeviceEndpoint $deviceEndpoint)
                            $captureProcess = Start-Process -FilePath $hostExe -ArgumentList $playbackArgs -WorkingDirectory $rootPath -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
                            $playbackPriority = Set-StreamingProcessPriority $captureProcess
                            $activeProfileName = $profileName
                            $activeDeviceEndpoint = $deviceEndpoint
                        }
                        $airplayStdout = Join-Path $live 'airplay-stdout.log'
                        $airplayStderr = Join-Path $live 'airplay-stderr.log'
                        if ($startPlan.RestartAirPlay) {
                            $activeLanInterface = Get-AirPlayLanInterface
                            $airplayArgs = @('-n', 'StemStudio', '-nh', '-vs', '0', '-stem-live-dir', 'data/live')
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
                        Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='airplay_waiting'; input_source='airplay'; monitor_stem='mix'; profile_name=$profileName; track_count=$trackCount; device_endpoint=$(if ($deviceEndpoint) { $deviceEndpoint } else { $null }); host_pid=$captureProcess.Id; airplay_pid=$airplayProcess.Id; playback_priority=$playbackPriority; airplay_priority=$airplayPriority; lan_interface=$(if ($null -ne $activeLanInterface) { $activeLanInterface.Name } else { $null }); lan_ipv4=$(if ($null -ne $activeLanInterface) { $activeLanInterface.IPv4 } else { $null }); connection_reused=(-not $startPlan.RestartAirPlay) })
                    } elseif ($command.action -eq 'start') {
                        Stop-Capture
                        $target = Get-Process -Id ([int]$command.process_id) -ErrorAction Stop
                        $stdout = Join-Path $live 'capture-stdout.log'
                        $stderr = Join-Path $live 'capture-stderr.log'
                        $profileName = [string]$command.profile_name
                        $trackCount = Assert-LiveTrackCount ([int]$command.track_count)
                        $deviceEndpoint = ''
                        if ($null -ne $command.PSObject.Properties['device_endpoint']) {
                            $deviceEndpoint = Assert-DeviceAudioEndpoint ([string]$command.device_endpoint)
                        }
                        $captureArgs = @(New-AudioHostArgumentList -Source ([string]$target.Id) -LiveDirectory 'data\live' -TrackCount $trackCount -DeviceEndpoint $deviceEndpoint)
                        $captureProcess = Start-Process -FilePath $hostExe -ArgumentList $captureArgs -WorkingDirectory $rootPath -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
                        $playbackPriority = Set-StreamingProcessPriority $captureProcess
                        $activeProfileName = $profileName
                        $activeDeviceEndpoint = $deviceEndpoint
                        Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='capturing'; process_id=$target.Id; process_name=$target.ProcessName; monitor_stem='mix'; profile_name=$profileName; track_count=$trackCount; device_endpoint=$(if ($deviceEndpoint) { $deviceEndpoint } else { $null }); host_pid=$captureProcess.Id; playback_priority=$playbackPriority })
                    } elseif ($command.action -eq 'stop') {
                        Stop-Capture
                        Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='stopped' })
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
            Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='error'; error="音频播放宿主已退出，退出码 $exitCode；AirPlay 接收器保持运行"; airplay_pid=$(if ($null -ne $airplayProcess -and -not $airplayProcess.HasExited) { $airplayProcess.Id } else { $null }) })
        }
        if ($null -ne $airplayProcess -and $airplayProcess.HasExited) {
            $exitCode = $airplayProcess.ExitCode
            Stop-Capture
            Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='error'; error="AirPlay 宿主已退出，退出码 $exitCode" })
        }
        Start-Sleep -Seconds 2
    }
} finally {
    Remove-Item -LiteralPath $controllerHeartbeat -Force -ErrorAction SilentlyContinue
    Stop-Capture
}
