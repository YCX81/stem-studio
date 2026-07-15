param(
    [string]$PackageRoot = '',
    [ValidateRange(1, 8)][int]$BuildJobs = 1
)

$ErrorActionPreference = 'Stop'
# Developer builds share CPU and project-volume I/O with the live separation
# pipeline. Keep the entire build tree below streaming priority; child compiler
# processes inherit this class on Windows.
(Get-Process -Id $PID).PriorityClass = 'BelowNormal'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$bash = 'C:\msys64\usr\bin\bash.exe'
$ucrtBin = 'C:\msys64\ucrt64\bin'
$objdump = Join-Path $ucrtBin 'objdump.exe'
if (-not (Test-Path -LiteralPath $bash)) { throw 'MSYS2 was not found at C:\msys64.' }
if (-not (Test-Path -LiteralPath $objdump)) { throw 'The MSYS2 UCRT64 GCC toolchain is incomplete.' }

$driveLetter = @('S','R','Q','P') | Where-Object { -not (Test-Path "$($_):\") } | Select-Object -First 1
if (-not $driveLetter) { throw 'No temporary ASCII drive letter is available (S/R/Q/P).' }
$drive = "$driveLetter`:"
$mappedRoot = "$drive\"
& subst.exe $drive $root
if ($LASTEXITCODE -ne 0) { throw "Could not map the project to $drive" }

try {
    $msysRoot = "/$($driveLetter.ToLowerInvariant())"
    $msysHome = Join-Path $root 'data\temp\msys-home'
    $msysTemp = Join-Path $root 'data\temp\msys-tmp'
    New-Item -ItemType Directory -Force -Path $msysHome, $msysTemp | Out-Null
    $env:HOME = "$drive\data\temp\msys-home"
    $env:TMP = "$drive\data\temp\msys-tmp"
    $env:TEMP = $env:TMP
    $buildCommand = @"
export HOME=$msysRoot/data/temp/msys-home TMP=$msysRoot/data/temp/msys-tmp TEMP=$msysRoot/data/temp/msys-tmp PATH=/ucrt64/bin:/usr/bin:`$PATH LANG=C.UTF-8
cd $msysRoot
cmake -S third_party/UxPlay -B data/temp/airplay-build -G Ninja -DNO_MARCH_NATIVE=ON -DCMAKE_BUILD_TYPE=Release
cmake --build data/temp/airplay-build --parallel $BuildJobs
"@
    & $bash -lc $buildCommand
    if ($LASTEXITCODE -ne 0) { throw "AirPlay host build failed with exit code $LASTEXITCODE." }
} finally {
    & subst.exe $drive /D | Out-Null
}

$package = if ($PackageRoot) {
    [System.IO.Path]::GetFullPath($PackageRoot)
} else {
    Join-Path $root 'airplay-host'
}
$bin = Join-Path $package 'bin'
$plugins = Join-Path $package 'lib\gstreamer-1.0'
$scannerDir = Join-Path $package 'libexec\gstreamer-1.0'
$docs = Join-Path $package 'share\doc\UxPlay'
New-Item -ItemType Directory -Force -Path $bin, $plugins, $scannerDir, $docs | Out-Null

$builtExe = Join-Path $root 'data\temp\airplay-build\stem-studio-airplay-host.exe'
Copy-Item -LiteralPath $builtExe -Destination $bin -Force
$pluginNames = @(
    'libgstapp.dll',
    'libgstaudiotestsrc.dll',
    'libgstaudioconvert.dll',
    'libgstaudioresample.dll',
    'libgstautodetect.dll',
    'libgstcoreelements.dll',
    'libgstlibav.dll',
    'libgstplayback.dll',
    'libgstvideoparsersbad.dll',
    'libgstvolume.dll'
)
$pluginSources = foreach ($name in $pluginNames) {
    $source = Join-Path 'C:\msys64\ucrt64\lib\gstreamer-1.0' $name
    if (-not (Test-Path -LiteralPath $source)) { throw "Missing GStreamer plugin: $name" }
    Copy-Item -LiteralPath $source -Destination $plugins -Force
    $source
}
$scanner = 'C:\msys64\ucrt64\libexec\gstreamer-1.0\gst-plugin-scanner.exe'
Copy-Item -LiteralPath $scanner -Destination $scannerDir -Force

$seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
$queue = [System.Collections.Generic.Queue[string]]::new()
& subst.exe $drive $root
if ($LASTEXITCODE -ne 0) { throw "Could not remap the project to $drive for dependency scanning." }
try {
    $builtExeForTools = "$drive\data\temp\airplay-build\stem-studio-airplay-host.exe"
    @($builtExeForTools) + @($pluginSources) + @($scanner) | ForEach-Object { $queue.Enqueue($_) }
    while ($queue.Count -gt 0) {
        $binary = $queue.Dequeue()
        foreach ($line in (& $objdump -p $binary 2>$null)) {
            if ($line -notmatch '^\s*DLL Name:\s*(.+?)\s*$') { continue }
            $name = $Matches[1]
            $dependency = Join-Path $ucrtBin $name
            if (-not (Test-Path -LiteralPath $dependency)) { continue }
            if ($seen.Add($name)) {
                Copy-Item -LiteralPath $dependency -Destination $bin -Force
                $queue.Enqueue($dependency)
            }
        }
    }
} finally {
    & subst.exe $drive /D | Out-Null
}

Copy-Item -LiteralPath (Join-Path $root 'third_party\UxPlay\LICENSE') -Destination $docs -Force
Copy-Item -LiteralPath (Join-Path $root 'third_party\UxPlay\STEM_STUDIO_UPSTREAM.md') -Destination $docs -Force
Write-Host "AirPlay host built and packaged: $package" -ForegroundColor Green
Write-Host "Runtime DLLs: $($seen.Count); GStreamer plugins: $($pluginNames.Count)"
