function Get-NormalizedFullPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Test-WorkspaceContainedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $rootPath = Get-NormalizedFullPath $Root
    $candidatePath = Get-NormalizedFullPath $Path
    if ($candidatePath.Equals($rootPath, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = $rootPath + [System.IO.Path]::DirectorySeparatorChar
    return $candidatePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Get-DirectoryManifestHash {
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootPath = Get-NormalizedFullPath $Root
    if (-not (Test-Path -LiteralPath $rootPath -PathType Container)) {
        throw "Manifest root was not found: $rootPath"
    }
    $prefix = $rootPath + [System.IO.Path]::DirectorySeparatorChar
    $builder = New-Object System.Text.StringBuilder
    $files = @(Get-ChildItem -LiteralPath $rootPath -File -Recurse | Sort-Object FullName)
    foreach ($file in $files) {
        $relativePath = $file.FullName.Substring($prefix.Length).Replace('\', '/')
        $fileHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToUpperInvariant()
        [void]$builder.Append($relativePath)
        [void]$builder.Append('|')
        [void]$builder.Append([string]$file.Length)
        [void]$builder.Append('|')
        [void]$builder.Append($fileHash)
        [void]$builder.Append("`n")
    }

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($builder.ToString())
        $digest = $algorithm.ComputeHash($bytes)
        return (($digest | ForEach-Object { $_.ToString('X2') }) -join '')
    } finally {
        $algorithm.Dispose()
    }
}

function Get-NativeHostInstallLayout {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9._-]+$')][string]$TransactionId
    )

    $rootPath = Get-NormalizedFullPath $Root
    $transactionRoot = Join-Path $rootPath ("data\temp\native-update-$TransactionId")
    $layout = [pscustomobject]@{
        Root = $rootPath
        TransactionRoot = $transactionRoot
        StageRoot = Join-Path $transactionRoot 'stage'
        BackupRoot = Join-Path $transactionRoot 'backup'
        AudioLive = Join-Path $rootPath 'host\bin\stem-studio-audio-host.exe'
        AirPlayLive = Join-Path $rootPath 'airplay-host'
        ControllerPidPath = Join-Path $rootPath 'data\live\controller.pid'
        ControllerStatusPath = Join-Path $rootPath 'data\live\controller-status.json'
        CommandPath = Join-Path $rootPath 'data\live\command.json'
        StatePath = Join-Path $rootPath 'data\temp\native-update-state.json'
    }
    foreach ($path in @(
        $layout.TransactionRoot,
        $layout.StageRoot,
        $layout.BackupRoot,
        $layout.AudioLive,
        $layout.AirPlayLive,
        $layout.ControllerPidPath,
        $layout.ControllerStatusPath,
        $layout.CommandPath,
        $layout.StatePath
    )) {
        if (-not (Test-WorkspaceContainedPath -Root $rootPath -Path $path)) {
            throw "Native update path escaped the workspace: $path"
        }
    }
    return $layout
}

function Test-AirPlayPcmQuiet {
    param(
        [Parameter(Mandatory = $true)][long]$BeforePcmFrames,
        [Parameter(Mandatory = $true)][long]$AfterPcmFrames
    )

    return $BeforePcmFrames -eq $AfterPcmFrames
}

function Get-NativeCommandSequence {
    param(
        [Parameter(Mandatory = $true)][long]$CurrentSequence,
        [DateTime]$NowUtc = [DateTime]::UtcNow
    )

    if ($CurrentSequence -eq [long]::MaxValue) {
        throw 'The native command sequence cannot be advanced.'
    }
    $milliseconds = ([DateTimeOffset]$NowUtc.ToUniversalTime()).ToUnixTimeMilliseconds()
    $clockSequence = [long]($milliseconds * 1000000L)
    return [Math]::Max($CurrentSequence + 1L, $clockSequence)
}

function New-NativeUpdateReadinessResult {
    param(
        [Parameter(Mandatory = $true)][bool]$Ready,
        [Parameter(Mandatory = $true)][string]$Code,
        [string]$Message = '',
        [string]$AudioHash = '',
        [string]$AirPlayHash = '',
        [Nullable[double]]$StatusAgeSeconds = $null
    )

    return [pscustomobject]@{
        Ready = $Ready
        Code = $Code
        Message = $Message
        AudioHash = $AudioHash
        AirPlayHash = $AirPlayHash
        StatusAgeSeconds = $StatusAgeSeconds
    }
}

function Get-NativeUpdateReadiness {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$AudioCandidate,
        [Parameter(Mandatory = $true)][string]$AirPlayCandidateRoot,
        [string]$ExpectedAudioHash = '',
        [string]$ExpectedAirPlayHash = '',
        [DateTime]$NowUtc = [DateTime]::UtcNow,
        [ValidateRange(1, 300)][int]$StreamingStatusMaxAgeSeconds = 5
    )

    $rootPath = Get-NormalizedFullPath $Root
    $audioPath = [System.IO.Path]::GetFullPath($AudioCandidate)
    $airplayRootPath = [System.IO.Path]::GetFullPath($AirPlayCandidateRoot)
    $airplayExePath = Join-Path $airplayRootPath 'bin\stem-studio-airplay-host.exe'

    foreach ($candidatePath in @($audioPath, $airplayRootPath, $airplayExePath)) {
        if (-not (Test-WorkspaceContainedPath -Root $rootPath -Path $candidatePath)) {
            return New-NativeUpdateReadinessResult -Ready $false -Code 'CandidateOutsideRoot' `
                -Message "Candidate escaped the workspace: $candidatePath"
        }
    }
    if (-not (Test-Path -LiteralPath $audioPath -PathType Leaf)) {
        return New-NativeUpdateReadinessResult -Ready $false -Code 'MissingAudioCandidate' `
            -Message "Audio host candidate was not found: $audioPath"
    }
    if (-not (Test-Path -LiteralPath $airplayExePath -PathType Leaf)) {
        return New-NativeUpdateReadinessResult -Ready $false -Code 'MissingAirPlayCandidate' `
            -Message "AirPlay host candidate was not found: $airplayExePath"
    }

    $audioHash = (Get-FileHash -LiteralPath $audioPath -Algorithm SHA256).Hash.ToUpperInvariant()
    $airplayHash = (Get-FileHash -LiteralPath $airplayExePath -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($ExpectedAudioHash -and $audioHash -ne $ExpectedAudioHash.ToUpperInvariant()) {
        return New-NativeUpdateReadinessResult -Ready $false -Code 'AudioHashMismatch' `
            -Message 'Audio host candidate hash did not match the expected hash.' `
            -AudioHash $audioHash -AirPlayHash $airplayHash
    }
    if ($ExpectedAirPlayHash -and $airplayHash -ne $ExpectedAirPlayHash.ToUpperInvariant()) {
        return New-NativeUpdateReadinessResult -Ready $false -Code 'AirPlayHashMismatch' `
            -Message 'AirPlay host candidate hash did not match the expected hash.' `
            -AudioHash $audioHash -AirPlayHash $airplayHash
    }

    $statusPath = Join-Path $rootPath 'data\live\airplay-status.json'
    $statusAge = $null
    if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
        try {
            $status = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $statusAge = [Math]::Max(
                0.0,
                ($NowUtc.ToUniversalTime() - (Get-Item -LiteralPath $statusPath).LastWriteTimeUtc).TotalSeconds
            )
            if ([string]$status.state -eq 'streaming' -and
                $statusAge -le $StreamingStatusMaxAgeSeconds) {
                return New-NativeUpdateReadinessResult -Ready $false -Code 'ActiveStream' `
                    -Message 'AirPlay PCM is still active.' `
                    -AudioHash $audioHash -AirPlayHash $airplayHash `
                    -StatusAgeSeconds $statusAge
            }
        } catch {
            return New-NativeUpdateReadinessResult -Ready $false -Code 'InvalidAirPlayStatus' `
                -Message $_.Exception.Message -AudioHash $audioHash -AirPlayHash $airplayHash
        }
    }

    return New-NativeUpdateReadinessResult -Ready $true -Code 'Ready' `
        -Message 'Native hosts are ready for a controlled update.' `
        -AudioHash $audioHash -AirPlayHash $airplayHash `
        -StatusAgeSeconds $statusAge
}
