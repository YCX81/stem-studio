param([Parameter(Mandatory = $true)][string]$Root)

$ErrorActionPreference = 'Stop'
$rootPath = [System.IO.Path]::GetFullPath($Root)
$live = Join-Path $rootPath 'data\live'
$hostExe = Join-Path $rootPath 'host\bin\stem-studio-audio-host.exe'
$lastCommand = 0L
$captureProcess = $null
$audioScanCountdown = 0

function Write-AtomicJson([string]$Path, $Value) {
    $partial = "$Path.part"
    ConvertTo-Json -InputObject $Value -Depth 4 | Set-Content -LiteralPath $partial -Encoding UTF8
    Move-Item -LiteralPath $partial -Destination $Path -Force
}

function Stop-Capture {
    if ($null -ne $script:captureProcess -and -not $script:captureProcess.HasExited) {
        Stop-Process -Id $script:captureProcess.Id -Force
        $script:captureProcess.WaitForExit(3000) | Out-Null
    }
    $script:captureProcess = $null
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

New-Item -ItemType Directory -Force -Path $live | Out-Null
if (-not (Test-Path -LiteralPath $hostExe)) { throw "缺少原生音频宿主：$hostExe" }
$existingCommandPath = Join-Path $live 'command.json'
if (Test-Path -LiteralPath $existingCommandPath) {
    try {
        $existingCommand = Get-Content -LiteralPath $existingCommandPath -Raw | ConvertFrom-Json
        $lastCommand = [long]$existingCommand.sequence
    } catch {
        $lastCommand = 0L
    }
}
Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='stopped' })

try {
    while ($true) {
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
                $command = Get-Content -LiteralPath $commandPath -Raw | ConvertFrom-Json
                if ([long]$command.sequence -gt $lastCommand) {
                    $lastCommand = [long]$command.sequence
                    if ($command.action -eq 'open_audio_settings') {
                        Start-Process 'ms-settings:apps-volume'
                        Write-AtomicJson (Join-Path $live 'routing-action.json') ([ordered]@{ state='opened'; opened_at=[DateTime]::UtcNow.ToString('o') })
                    } elseif ($command.action -eq 'start') {
                        Stop-Capture
                        $target = Get-Process -Id ([int]$command.process_id) -ErrorAction Stop
                        $stdout = Join-Path $live 'capture-stdout.log'
                        $stderr = Join-Path $live 'capture-stderr.log'
                        $allowedStems = @('instrumental', 'vocals', 'drums', 'bass', 'other', 'guitar', 'piano')
                        $stem = [string]$command.monitor_stem
                        if ($allowedStems -notcontains $stem) { throw "不支持的监听音轨：$stem" }
                        $profileName = [string]$command.profile_name
                        $captureProcess = Start-Process -FilePath $hostExe -ArgumentList @($target.Id, $live, $stem) -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
                        Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='capturing'; process_id=$target.Id; process_name=$target.ProcessName; monitor_stem=$stem; profile_name=$profileName; host_pid=$captureProcess.Id })
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
            Write-AtomicJson (Join-Path $live 'controller-status.json') ([ordered]@{ state='error'; error="音频宿主已退出，退出码 $($captureProcess.ExitCode)" })
            $captureProcess = $null
        }
        Start-Sleep -Seconds 2
    }
} finally {
    Stop-Capture
}
