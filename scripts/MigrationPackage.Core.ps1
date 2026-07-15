. (Join-Path $PSScriptRoot 'NativeHostUpdate.Core.ps1')

function Get-MigrationPackageItems {
    param([switch]$IncludeModels)

    $items = @(
        'src',
        'scripts',
        'host',
        'airplay-host',
        'third_party/UxPlay',
        'docs',
        'tools',
        'Dockerfile',
        'compose.yaml',
        'pyproject.toml',
        'README.md',
        '.dockerignore',
        '.gitignore'
    )
    if ($IncludeModels) { $items += 'data/models' }
    return $items
}

function Remove-MigrationExcludedArtifacts {
    param(
        [Parameter(Mandatory = $true)][string]$StagingRoot,
        [Parameter(Mandatory = $true)][string]$AllowedRoot
    )

    $stagingPath = [System.IO.Path]::GetFullPath($StagingRoot)
    $allowedPath = [System.IO.Path]::GetFullPath($AllowedRoot)
    if (-not (Test-WorkspaceContainedPath -Root $allowedPath -Path $stagingPath)) {
        throw "Migration staging path escaped its allowed root: $stagingPath"
    }
    if (-not (Test-Path -LiteralPath $stagingPath -PathType Container)) { return 0 }

    $removed = 0
    $hostBin = Join-Path $stagingPath 'host\bin'
    if (Test-Path -LiteralPath $hostBin -PathType Container) {
        foreach ($candidate in @(Get-ChildItem -LiteralPath $hostBin -File -Filter '*.next.exe')) {
            if (-not (Test-WorkspaceContainedPath -Root $stagingPath -Path $candidate.FullName)) {
                throw "Migration cleanup candidate escaped staging: $($candidate.FullName)"
            }
            Remove-Item -LiteralPath $candidate.FullName -Force
            $removed++
        }
    }
    return $removed
}

function Get-MigrationNativeRuntimeManifest {
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootPath = [System.IO.Path]::GetFullPath($Root)
    $audioHost = Join-Path $rootPath 'host\bin\stem-studio-audio-host.exe'
    $airplayRoot = Join-Path $rootPath 'airplay-host'
    $airplayHost = Join-Path $airplayRoot 'bin\stem-studio-airplay-host.exe'
    if (-not (Test-Path -LiteralPath $audioHost -PathType Leaf)) {
        throw "Migration audio host was not found: $audioHost"
    }
    if (-not (Test-Path -LiteralPath $airplayHost -PathType Leaf)) {
        throw "Migration AirPlay host was not found: $airplayHost"
    }
    return [ordered]@{
        audio_host_sha256 = (Get-FileHash -LiteralPath $audioHost -Algorithm SHA256).Hash.ToUpperInvariant()
        airplay_host_sha256 = (Get-FileHash -LiteralPath $airplayHost -Algorithm SHA256).Hash.ToUpperInvariant()
        airplay_package_sha256 = Get-DirectoryManifestHash -Root $airplayRoot
    }
}
