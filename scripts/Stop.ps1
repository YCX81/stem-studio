$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$hostId = $null
if (Test-Path -LiteralPath 'data\live\controller-status.json') {
    try {
        $status = Get-Content -LiteralPath 'data\live\controller-status.json' -Raw | ConvertFrom-Json
        if ($status.host_pid) { $hostId = [int]$status.host_pid }
        if ($status.airplay_pid) { Stop-Process -Id ([int]$status.airplay_pid) -Force -ErrorAction SilentlyContinue }
    } catch { }
}
if ($null -ne $hostId) {
    $hostProcess = Get-Process -Id $hostId -ErrorAction SilentlyContinue
    if ($null -ne $hostProcess) {
        try {
            $stopEvent = [System.Threading.EventWaitHandle]::OpenExisting("Local\StemStudioAudioHostStop-$hostId")
            try { $stopEvent.Set() | Out-Null } finally { $stopEvent.Dispose() }
            if (-not $hostProcess.WaitForExit(5000)) {
                Stop-Process -Id $hostId -Force -ErrorAction SilentlyContinue
            }
        } catch {
            Stop-Process -Id $hostId -Force -ErrorAction SilentlyContinue
        }
    }
}
if (Test-Path -LiteralPath 'data\live\controller.pid') {
    $controllerId = [int](Get-Content -LiteralPath 'data\live\controller.pid' -Raw)
    Stop-Process -Id $controllerId -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath 'data\live\controller.pid' -Force
}
docker compose down
