param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [string]$SourceRoot = '',
    [switch]$IncludeModels,
    [switch]$IncludeImage
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'MigrationPackage.Core.ps1')
$root = if ($SourceRoot) {
    [System.IO.Path]::GetFullPath($SourceRoot)
} else {
    Split-Path -Parent $PSScriptRoot
}
$destinationPath = [System.IO.Path]::GetFullPath($Destination)
$archiveName = if ($IncludeModels) { 'StemStudio-with-models.zip' } else { 'StemStudio.zip' }
$archivePath = Join-Path $destinationPath $archiveName
New-Item -ItemType Directory -Force -Path $destinationPath | Out-Null

$items = @(Get-MigrationPackageItems -IncludeModels:$IncludeModels)
$nativeRuntimeManifest = $null
$removedCandidates = 0

Push-Location $root
try {
    if (Test-Path $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    $stagingPath = Join-Path $destinationPath ('.stem-studio-export-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $stagingPath | Out-Null
    foreach ($relativePath in $items) {
        $sourcePath = Join-Path $root $relativePath
        if (-not (Test-Path -LiteralPath $sourcePath)) {
            throw "迁移源文件不存在：$relativePath"
        }
        $targetPath = Join-Path $stagingPath $relativePath
        if (Test-Path -LiteralPath $sourcePath -PathType Container) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $targetPath) | Out-Null
            Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Recurse -Force
        } else {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $targetPath) | Out-Null
            Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Force
        }
    }
    Get-ChildItem -LiteralPath $stagingPath -Directory -Recurse -Filter '__pycache__' | Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $stagingPath -File -Recurse |
        Where-Object { $_.Extension -in @('.pyc', '.pyo') } |
        Remove-Item -Force
    $removedCandidates = Remove-MigrationExcludedArtifacts `
        -StagingRoot $stagingPath `
        -AllowedRoot $destinationPath
    $nativeRuntimeManifest = Get-MigrationNativeRuntimeManifest -Root $stagingPath
    $nativeRuntimeManifest | ConvertTo-Json -Depth 3 |
        Set-Content -LiteralPath (Join-Path $stagingPath 'native-runtime-manifest.json') -Encoding UTF8
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $stagingPath,
        $archivePath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )
} finally {
    if ($stagingPath -and (Test-Path -LiteralPath $stagingPath)) {
        $resolvedStaging = [System.IO.Path]::GetFullPath($stagingPath)
        if (-not $resolvedStaging.StartsWith($destinationPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "拒绝清理迁移目标之外的 staging 目录：$resolvedStaging"
        }
        Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
    }
    Pop-Location
}

Write-Host "迁移包已生成：$archivePath" -ForegroundColor Green
$archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
$archiveSidecar = "$archivePath.sha256"
Set-Content -LiteralPath $archiveSidecar -Value "$archiveHash  $archiveName" -Encoding ascii
Write-Host "迁移包 SHA256：$archiveHash"
if (-not $IncludeModels) {
    Write-Host '未包含模型缓存；目标电脑首次分离时会重新下载模型。'
}

if ($IncludeImage) {
    docker image inspect stem-studio:0.1.0 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw '本机尚未构建 stem-studio:0.1.0 镜像，请先运行 scripts\Start.ps1。'
    }
    $imagePath = Join-Path $destinationPath 'stem-studio-image.tar'
    docker save --output $imagePath stem-studio:0.1.0
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker 镜像导出失败。'
    }
    Write-Host "Docker 离线镜像已生成：$imagePath" -ForegroundColor Green
    $imageHash = (Get-FileHash -LiteralPath $imagePath -Algorithm SHA256).Hash
    Set-Content -LiteralPath "$imagePath.sha256" -Value "$imageHash  stem-studio-image.tar" -Encoding ascii
    Write-Host "Docker 镜像 SHA256：$imageHash"
}

$manifest = [ordered]@{
    product = 'Stem Studio'
    version = '0.1.0'
    created_at = [DateTime]::UtcNow.ToString('o')
    archive = $archiveName
    archive_sha256 = $archiveHash
    includes_models = [bool]$IncludeModels
    includes_image = [bool]$IncludeImage
    native_runtime = $nativeRuntimeManifest
    excluded_next_host_candidates = $removedCandidates
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $destinationPath 'migration-manifest.json') -Encoding UTF8
