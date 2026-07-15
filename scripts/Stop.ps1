$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (Test-Path -LiteralPath 'data\live\controller-status.json') {
    try {
        $status = Get-Content -LiteralPath 'data\live\controller-status.json' -Raw | ConvertFrom-Json
        if ($status.host_pid) { Stop-Process -Id ([int]$status.host_pid) -Force -ErrorAction SilentlyContinue }
        if ($status.airplay_pid) { Stop-Process -Id ([int]$status.airplay_pid) -Force -ErrorAction SilentlyContinue }
    } catch { }
}
if (Test-Path -LiteralPath 'data\live\controller.pid') {
    $controllerId = [int](Get-Content -LiteralPath 'data\live\controller.pid' -Raw)
    Stop-Process -Id $controllerId -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath 'data\live\controller.pid' -Force
}
docker compose down
