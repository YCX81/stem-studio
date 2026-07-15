param(
    [string]$Root = '',
    [string]$ManifestPath = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'MigrationPackage.Core.ps1')

$rootPath = if ($Root) {
    [System.IO.Path]::GetFullPath($Root)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
}
$manifestFile = if ($ManifestPath) {
    [System.IO.Path]::GetFullPath($ManifestPath)
} else {
    Join-Path $rootPath 'migration-manifest.json'
}
if (-not (Test-Path -LiteralPath $manifestFile -PathType Leaf)) {
    throw "Migration manifest was not found: $manifestFile"
}

$manifest = Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
$nativeRuntimeManifest = if ($null -ne $manifest.native_runtime) {
    $manifest.native_runtime
} elseif ($manifest.audio_host_sha256 -and
    $manifest.airplay_host_sha256 -and
    $manifest.airplay_package_sha256) {
    $manifest
} else {
    throw 'Migration manifest does not contain native_runtime hashes.'
}
$verification = Test-MigrationNativeRuntimeManifest `
    -Root $rootPath `
    -NativeRuntimeManifest $nativeRuntimeManifest
if (-not $verification.Valid) {
    $details = @($verification.Mismatches | ForEach-Object {
        "$($_.component): expected=$($_.expected), actual=$($_.actual)"
    }) -join '; '
    throw "Native runtime verification failed: $details"
}

$verification | ConvertTo-Json -Depth 5
