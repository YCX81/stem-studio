param(
    [switch]$NoBuild,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$hostExe = Join-Path $root 'host\bin\stem-studio-audio-host.exe'
if (-not (Test-Path -LiteralPath $hostExe)) {
    throw '缺少 host\bin\stem-studio-audio-host.exe，请先构建原生音频宿主。'
}
$airplayExe = Join-Path $root 'airplay-host\bin\stem-studio-airplay-host.exe'
if (-not (Test-Path -LiteralPath $airplayExe)) {
    throw '缺少 airplay-host\bin\stem-studio-airplay-host.exe，请先运行 scripts\Build-AirPlayHost.ps1。'
}
$nativeRuntimeManifest = Join-Path $root 'native-runtime-manifest.json'
if (Test-Path -LiteralPath $nativeRuntimeManifest -PathType Leaf) {
    & (Join-Path $PSScriptRoot 'Verify-NativeRuntime.ps1') `
        -Root $root `
        -ManifestPath $nativeRuntimeManifest | Out-Null
}

docker info | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker 服务未运行，请先启动 Docker Desktop。'
}

foreach ($directory in @('data/models', 'data/outputs', 'data/temp', 'data/live/inbox', 'data/live/outbox', 'data/live/work', 'data/live/failed')) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

if ($NoBuild) {
    docker compose up --detach --no-build
} else {
    docker compose up --build --detach
}
if ($LASTEXITCODE -ne 0) {
    throw '镜像构建或容器启动失败。请运行 docker compose logs app 查看日志。'
}

if (Test-Path -LiteralPath 'data\live\controller-status.json') {
    try {
        $oldStatus = Get-Content -LiteralPath 'data\live\controller-status.json' -Raw | ConvertFrom-Json
        if ($oldStatus.host_pid) { Stop-Process -Id ([int]$oldStatus.host_pid) -Force -ErrorAction SilentlyContinue }
        if ($oldStatus.airplay_pid) { Stop-Process -Id ([int]$oldStatus.airplay_pid) -Force -ErrorAction SilentlyContinue }
    } catch { }
}
if (Test-Path -LiteralPath 'data\live\controller.pid') {
    try { Stop-Process -Id ([int](Get-Content -LiteralPath 'data\live\controller.pid' -Raw)) -Force -ErrorAction SilentlyContinue } catch { }
    Remove-Item -LiteralPath 'data\live\controller.pid' -Force
}

$controller = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'HostController.ps1'), '-Root', $root) -WindowStyle Hidden -PassThru
$controller.Id | Set-Content -LiteralPath (Join-Path $root 'data\live\controller.pid') -Encoding ascii

Write-Host '正在等待 Stem Studio 启动...'
for ($attempt = 1; $attempt -le 60; $attempt++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:7860/' -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:7860/' }
            Write-Host 'Stem Studio 已启动：http://127.0.0.1:7860/' -ForegroundColor Green
            exit 0
        }
    } catch {
        Start-Sleep -Seconds 2
    }
}

docker compose logs --tail 80 app
throw 'Stem Studio 未能在规定时间内启动。'
