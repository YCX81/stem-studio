param(
    [Parameter(Mandatory = $true)]
    [string]$ImagePath
)

$ErrorActionPreference = 'Stop'
$resolvedImage = (Resolve-Path -LiteralPath $ImagePath).Path
$sidecar = "$resolvedImage.sha256"
if (Test-Path -LiteralPath $sidecar) {
    $expected = ((Get-Content -LiteralPath $sidecar -Raw).Trim() -split '\s+')[0].ToUpperInvariant()
    $actual = (Get-FileHash -LiteralPath $resolvedImage -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($expected -ne $actual) {
        throw "Docker 离线镜像 SHA256 不匹配。期望 $expected，实际 $actual。"
    }
    Write-Host 'Docker 离线镜像 SHA256 校验通过。' -ForegroundColor Green
}
docker info | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker 服务未运行，请先启动 Docker Desktop。'
}

docker load --input $resolvedImage
if ($LASTEXITCODE -ne 0) {
    throw 'Docker 离线镜像导入失败。'
}

& (Join-Path $PSScriptRoot 'Start.ps1') -NoBuild
