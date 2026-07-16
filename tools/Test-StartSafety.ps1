$ErrorActionPreference = 'Stop'

$startScript = Join-Path $PSScriptRoot '..\scripts\Start.ps1'
$source = Get-Content -LiteralPath $startScript -Raw -Encoding UTF8

$dockerCheck = $source.IndexOf('docker info | Out-Null', [StringComparison]::Ordinal)
$oldControllerCleanup = $source.IndexOf(
    "if (Test-Path -LiteralPath 'data\live\controller-status.json')",
    [StringComparison]::Ordinal
)
$controllerLaunch = $source.IndexOf(
    '$controller = Start-Process',
    [StringComparison]::Ordinal
)
$composeNoBuild = $source.IndexOf(
    'docker compose up --detach --no-build',
    [StringComparison]::Ordinal
)
$composeBuild = $source.IndexOf(
    'docker compose up --build --detach',
    [StringComparison]::Ordinal
)

if (
    $dockerCheck -lt 0 -or
    $oldControllerCleanup -lt 0 -or
    $controllerLaunch -lt 0 -or
    $composeNoBuild -lt 0 -or
    $composeBuild -lt 0
) {
    throw 'Start.ps1 no longer exposes the expected Docker, compose, cleanup, and controller launch steps.'
}
if ($dockerCheck -ge $oldControllerCleanup) {
    throw 'Docker readiness must be proven before stopping an existing controller or its hosts.'
}
if ($dockerCheck -ge $controllerLaunch) {
    throw 'Docker readiness must be proven before launching the Windows controller.'
}
if ($composeNoBuild -ge $oldControllerCleanup -or $composeBuild -ge $oldControllerCleanup) {
    throw 'Container startup must succeed before stopping an existing controller or its hosts.'
}
if ($composeNoBuild -ge $controllerLaunch -or $composeBuild -ge $controllerLaunch) {
    throw 'Container startup must succeed before launching the replacement Windows controller.'
}

'Start script Docker safety tests: PASS'
