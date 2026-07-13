param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath,
    [string]$ChecksumPath
)

$ErrorActionPreference = 'Stop'
$resolvedFile = (Resolve-Path -LiteralPath $FilePath).Path
$sidecar = if ($ChecksumPath) {
    (Resolve-Path -LiteralPath $ChecksumPath).Path
} else {
    "$resolvedFile.sha256"
}
if (-not (Test-Path -LiteralPath $sidecar)) {
    throw "缺少 SHA256 文件：$sidecar"
}
$expected = ((Get-Content -LiteralPath $sidecar -Raw).Trim() -split '\s+')[0].ToUpperInvariant()
$actual = (Get-FileHash -LiteralPath $resolvedFile -Algorithm SHA256).Hash.ToUpperInvariant()
if ($expected -ne $actual) {
    throw "SHA256 不匹配。期望 $expected，实际 $actual。"
}
Write-Host "SHA256 校验通过：$resolvedFile" -ForegroundColor Green
