$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot '..\scripts\MigrationPackage.Core.ps1')

function Assert-Equal($Expected, $Actual, [string]$Message) {
    if ($Expected -ne $Actual) {
        throw "$Message (expected=$Expected, actual=$Actual)"
    }
}

function Assert-True([bool]$Value, [string]$Message) {
    if (-not $Value) { throw $Message }
}

$workspace = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$testRoot = Join-Path $workspace ("data\temp\migration-policy-test-" + [Guid]::NewGuid().ToString('N'))

try {
    $defaultItems = @(Get-MigrationPackageItems)
    foreach ($required in @(
        'src',
        'scripts',
        'host',
        'airplay-host',
        'third_party/UxPlay',
        'docs',
        'tools',
        'README.md'
    )) {
        Assert-True ($required -in $defaultItems) "Migration package is missing required item: $required"
    }
    foreach ($privatePath in @('data/live', 'data/outputs', 'data/temp', 'data/models')) {
        Assert-Equal $false ($privatePath -in $defaultItems) `
            "Default migration package must exclude private or optional data: $privatePath"
    }
    $modelItems = @(Get-MigrationPackageItems -IncludeModels)
    Assert-True ('data/models' -in $modelItems) 'Model export must be explicit'

    $stagingRoot = Join-Path $testRoot 'staging'
    $hostBin = Join-Path $stagingRoot 'host\bin'
    $audioLive = Join-Path $hostBin 'stem-studio-audio-host.exe'
    $audioCandidate = Join-Path $hostBin 'stem-studio-audio-host.next.exe'
    $debugCandidate = Join-Path $hostBin 'alternate.next.exe'
    New-Item -ItemType Directory -Force -Path $hostBin | Out-Null
    [System.IO.File]::WriteAllBytes($audioLive, [byte[]](1, 2, 3, 4))
    [System.IO.File]::WriteAllBytes($audioCandidate, [byte[]](5, 6, 7, 8))
    [System.IO.File]::WriteAllBytes($debugCandidate, [byte[]](9, 10, 11, 12))

    $removed = Remove-MigrationExcludedArtifacts -StagingRoot $stagingRoot -AllowedRoot $testRoot
    Assert-Equal 2 $removed 'All staged next-host candidates must be removed'
    Assert-True (Test-Path -LiteralPath $audioLive) 'Installed audio host must remain in the package'
    Assert-Equal $false (Test-Path -LiteralPath $audioCandidate) 'Primary candidate must be excluded'
    Assert-Equal $false (Test-Path -LiteralPath $debugCandidate) 'Alternate candidate must be excluded'

    $escapedRejected = $false
    try {
        Remove-MigrationExcludedArtifacts `
            -StagingRoot (Join-Path (Split-Path -Parent $testRoot) 'outside') `
            -AllowedRoot $testRoot | Out-Null
    } catch {
        $escapedRejected = $true
    }
    Assert-True $escapedRejected 'Migration cleanup must reject paths outside its allowed root'

    $runtimeRoot = Join-Path $testRoot 'runtime'
    $runtimeAudio = Join-Path $runtimeRoot 'host\bin\stem-studio-audio-host.exe'
    $runtimeAirPlay = Join-Path $runtimeRoot 'airplay-host\bin\stem-studio-airplay-host.exe'
    $runtimePlugin = Join-Path $runtimeRoot 'airplay-host\lib\gstreamer-1.0\plugin.dll'
    foreach ($path in @($runtimeAudio, $runtimeAirPlay, $runtimePlugin)) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
    }
    [System.IO.File]::WriteAllBytes($runtimeAudio, [byte[]](13, 14, 15, 16))
    [System.IO.File]::WriteAllBytes($runtimeAirPlay, [byte[]](17, 18, 19, 20))
    [System.IO.File]::WriteAllBytes($runtimePlugin, [byte[]](21, 22, 23, 24))

    $runtimeManifest = Get-MigrationNativeRuntimeManifest -Root $runtimeRoot
    Assert-Equal (Get-FileHash -LiteralPath $runtimeAudio -Algorithm SHA256).Hash `
        $runtimeManifest.audio_host_sha256 'Audio host manifest hash'
    Assert-Equal (Get-FileHash -LiteralPath $runtimeAirPlay -Algorithm SHA256).Hash `
        $runtimeManifest.airplay_host_sha256 'AirPlay host manifest hash'
    Assert-Equal (Get-DirectoryManifestHash -Root (Join-Path $runtimeRoot 'airplay-host')) `
        $runtimeManifest.airplay_package_sha256 'Complete AirPlay package manifest hash'

    $validRuntime = Test-MigrationNativeRuntimeManifest `
        -Root $runtimeRoot `
        -NativeRuntimeManifest $runtimeManifest
    Assert-Equal $true $validRuntime.Valid 'Matching native runtime must pass verification'
    Assert-Equal 0 @($validRuntime.Mismatches).Count 'Matching runtime has no mismatch details'

    [System.IO.File]::WriteAllBytes($runtimePlugin, [byte[]](21, 22, 23, 25))
    $corruptRuntime = Test-MigrationNativeRuntimeManifest `
        -Root $runtimeRoot `
        -NativeRuntimeManifest $runtimeManifest
    Assert-Equal $false $corruptRuntime.Valid 'A changed GStreamer DLL must fail runtime verification'
    Assert-True ('airplay_package_sha256' -in @($corruptRuntime.Mismatches.component)) `
        'Runtime verification must identify the complete AirPlay package mismatch'

    $runtimeManifestPath = Join-Path $runtimeRoot 'migration-manifest.json'
    [ordered]@{ native_runtime = $runtimeManifest } | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath $runtimeManifestPath -Encoding UTF8
    $verifyRuntimeScript = Join-Path $workspace 'scripts\Verify-NativeRuntime.ps1'
    $wrapperRejected = $false
    try {
        & $verifyRuntimeScript -Root $runtimeRoot -ManifestPath $runtimeManifestPath | Out-Null
    } catch {
        $wrapperRejected = $_.Exception.Message -like '*airplay_package_sha256*'
    }
    Assert-True $wrapperRejected 'Runtime verification command must reject and name a corrupt package'
    [System.IO.File]::WriteAllBytes($runtimePlugin, [byte[]](21, 22, 23, 24))
    & $verifyRuntimeScript -Root $runtimeRoot -ManifestPath $runtimeManifestPath | Out-Null
    $directRuntimeManifestPath = Join-Path $runtimeRoot 'native-runtime-manifest.json'
    $runtimeManifest | ConvertTo-Json -Depth 3 |
        Set-Content -LiteralPath $directRuntimeManifestPath -Encoding UTF8
    & $verifyRuntimeScript -Root $runtimeRoot -ManifestPath $directRuntimeManifestPath | Out-Null

    $sourceRoot = Join-Path $testRoot 'source'
    $exportRoot = Join-Path $testRoot 'export'
    $sourceFiles = @(
        'Dockerfile',
        'compose.yaml',
        'pyproject.toml',
        'README.md',
        '.dockerignore',
        '.gitignore',
        'src\stemstudio\app.py',
        'scripts\Start.ps1',
        'host\bin\stem-studio-audio-host.exe',
        'host\bin\stem-studio-audio-host.next.exe',
        'airplay-host\bin\stem-studio-airplay-host.exe',
        'airplay-host\lib\gstreamer-1.0\plugin.dll',
        'third_party\UxPlay\LICENSE',
        'docs\product-audio-acceptance.md',
        'tools\monitor_live_acceptance.py',
        'data\live\private-song.wav',
        'data\models\optional-model.bin'
    )
    foreach ($relativePath in $sourceFiles) {
        $path = Join-Path $sourceRoot $relativePath
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
        [System.IO.File]::WriteAllBytes($path, [System.Text.Encoding]::UTF8.GetBytes($relativePath))
    }

    $exportScript = Join-Path $workspace 'scripts\Export-Migration.ps1'
    & $exportScript -Destination $exportRoot -SourceRoot $sourceRoot | Out-Null
    $archivePath = Join-Path $exportRoot 'StemStudio.zip'
    Assert-True (Test-Path -LiteralPath $archivePath) 'Integrated migration export must create its archive'
    Assert-True (Test-Path -LiteralPath "$archivePath.sha256") 'Integrated export must create a SHA256 sidecar'

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $entries = @($archive.Entries | ForEach-Object { $_.FullName.Replace('\', '/') })
    } finally {
        $archive.Dispose()
    }
    foreach ($requiredEntry in @(
        'docs/product-audio-acceptance.md',
        'tools/monitor_live_acceptance.py',
        'third_party/UxPlay/LICENSE',
        'host/bin/stem-studio-audio-host.exe',
        'airplay-host/bin/stem-studio-airplay-host.exe',
        'native-runtime-manifest.json'
    )) {
        Assert-True ($requiredEntry -in $entries) "Integrated export is missing: $requiredEntry"
    }
    Assert-Equal $false ('host/bin/stem-studio-audio-host.next.exe' -in $entries) `
        'Integrated export must exclude staged candidate executables'
    Assert-Equal $false ('data/live/private-song.wav' -in $entries) `
        'Integrated export must exclude private live audio'
    Assert-Equal $false ('data/models/optional-model.bin' -in $entries) `
        'Integrated default export must exclude optional models'

    $exportManifest = Get-Content -LiteralPath (Join-Path $exportRoot 'migration-manifest.json') `
        -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Equal 1 $exportManifest.excluded_next_host_candidates `
        'Integrated manifest records excluded candidate count'
    Assert-Equal $false ('source_machine' -in @($exportManifest.PSObject.Properties.Name)) `
        'Migration manifest must not expose the source computer name'
    Assert-Equal (Get-FileHash -LiteralPath (Join-Path $sourceRoot 'host\bin\stem-studio-audio-host.exe') -Algorithm SHA256).Hash `
        $exportManifest.native_runtime.audio_host_sha256 'Integrated manifest audio host hash'
    Assert-Equal (Get-FileHash -LiteralPath (Join-Path $sourceRoot 'airplay-host\bin\stem-studio-airplay-host.exe') -Algorithm SHA256).Hash `
        $exportManifest.native_runtime.airplay_host_sha256 'Integrated manifest AirPlay host hash'

    'Migration package policy tests: PASS'
} finally {
    $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
    if ((Test-Path -LiteralPath $resolvedTestRoot) -and
        (Test-WorkspaceContainedPath -Root $workspace -Path $resolvedTestRoot)) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
