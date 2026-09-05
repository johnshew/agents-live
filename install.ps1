[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Version
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$embeddedVersion = ''
if (-not $Version) { $Version = $embeddedVersion }

$apiRoot = if ($env:AGENTS_LIVE_RELEASE_API) {
    $env:AGENTS_LIVE_RELEASE_API.TrimEnd('/')
} else {
    'https://api.github.com/repos/johnshew/agents-live/releases'
}
$downloadRoot = if ($env:AGENTS_LIVE_RELEASE_DOWNLOAD_ROOT) {
    $env:AGENTS_LIVE_RELEASE_DOWNLOAD_ROOT.TrimEnd('/')
} else {
    'https://github.com/johnshew/agents-live/releases/download'
}

function Get-ReleaseJson([string]$Url) {
    try {
        Invoke-RestMethod -Uri $Url -Headers @{
            Accept = 'application/vnd.github+json'
            'User-Agent' = 'agents-live-bootstrap'
            'X-GitHub-Api-Version' = '2022-11-28'
        } -UseBasicParsing
    } catch {
        throw "Could not retrieve release metadata from $Url. Check proxy and TLS settings. $($_.Exception.Message)"
    }
}

function Get-ReleaseAsset($Release, [string]$Name, [string]$ResolvedVersion) {
    # Not $matches: that name is PowerShell's automatic match variable.
    $candidates = @($Release.assets | Where-Object { $_.name -eq $Name })
    if ($candidates.Count -ne 1) {
        throw "Release v$ResolvedVersion does not contain exactly one $Name asset."
    }
    $asset = $candidates[0]
    $expectedUrl = "$downloadRoot/v$ResolvedVersion/$Name"
    $assetUrl = [Uri]::UnescapeDataString([string]$asset.browser_download_url)
    if ($asset.state -ne 'uploaded' -or $assetUrl -ne $expectedUrl -or
        $asset.digest -notmatch '^sha256:[0-9a-f]{64}$' -or [long]$asset.size -le 0) {
        throw "Release v$ResolvedVersion has incomplete or invalid provenance for $Name."
    }
    $asset
}

function Save-VerifiedAsset($Asset, [string]$Destination) {
    try {
        Invoke-WebRequest -Uri $Asset.browser_download_url -OutFile $Destination -UseBasicParsing
    } catch {
        throw "Could not download $($Asset.browser_download_url). Check proxy and TLS settings. No package-index fallback was used. $($_.Exception.Message)"
    }
    $file = Get-Item -LiteralPath $Destination
    if ($file.Length -ne [long]$Asset.size) {
        throw "$($Asset.name) was $($file.Length) bytes, expected $($Asset.size)."
    }
    $expected = $Asset.digest.Substring(7)
    $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "$($Asset.name) checksum mismatch: expected $expected, got $actual."
    }
}

$stableVersion = '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
$releaseVersion = '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:(?:a|b|rc)[0-9]+|\.dev[0-9]+)?(?:\+[0-9A-Za-z]+(?:[._-][0-9A-Za-z]+)*)?$'
if ($Version -and $Version -notmatch $releaseVersion) {
    throw "'$Version' is not an exact stable or prerelease version."
}
$metadataUrl = if ($Version) { "$apiRoot/tags/v$Version" } else { "$apiRoot/latest" }
$release = Get-ReleaseJson $metadataUrl
$resolved = [string]$release.tag_name
if ($resolved -notmatch "^v$($releaseVersion.Substring(1))" -or $release.draft -ne $false) {
    throw "GitHub release metadata does not identify a published release."
}
$resolved = $resolved.Substring(1)
if ($Version -and $resolved -ne $Version) {
    throw "GitHub returned release $resolved, expected exactly $Version."
}
$expectsPrerelease = $Version -and $Version -notmatch $stableVersion
if (($expectsPrerelease -and $release.prerelease -ne $true) -or
    (-not $expectsPrerelease -and $release.prerelease -ne $false)) {
    throw "GitHub release v$resolved has the wrong prerelease status."
}

$wheelName = "agents_live-$resolved-py3-none-any.whl"
$wheelAsset = Get-ReleaseAsset $release $wheelName $resolved

$temporary = Join-Path ([IO.Path]::GetTempPath()) ("agents-live-install-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    $wheel = Join-Path $temporary $wheelName
    Save-VerifiedAsset $wheelAsset $wheel

    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uvCommand) {
        try {
            $uvInstaller = Join-Path $temporary 'uv-install.ps1'
            Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -OutFile $uvInstaller -UseBasicParsing
            & $uvInstaller
        } catch {
            throw "Could not install the required uv bootstrap runtime. Check proxy and TLS settings. $($_.Exception.Message)"
        }
        $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
        if (-not $uvCommand) {
            $candidate = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
            if (Test-Path -LiteralPath $candidate) { $uvCommand = Get-Item $candidate }
        }
    }
    if (-not $uvCommand) { throw 'uv installation completed but uv.exe was not found.' }

    $installRoot = if ($env:AGENTS_LIVE_INSTALL_ROOT) {
        [Environment]::ExpandEnvironmentVariables($env:AGENTS_LIVE_INSTALL_ROOT)
    } else {
        Join-Path $env:LOCALAPPDATA 'agents-live'
    }
    # The bootstrap authenticates bytes and hands them to the package. It does
    # not know the installation layout: the wheel's own install-release builds,
    # validates, seals, and activates the generation.
    & $uvCommand.Source tool run --isolated --from $wheel `
        agents-live install-release $resolved `
        --install-root $installRoot --activate --wheel $wheel
    if ($LASTEXITCODE -ne 0) { throw "Agents Live installation failed with exit code $LASTEXITCODE." }

    # Only after the new installation answers: a uv-managed one would otherwise
    # keep answering to agents-live on PATH alongside it.
    $uvTools = & $uvCommand.Source tool list 2>$null
    if ($uvTools -and ($uvTools | Where-Object { $_ -match '^agents-live\s' })) {
        & $uvCommand.Source tool uninstall agents-live 2>$null | Out-Null
    }
    $executable = Join-Path $installRoot 'current\Scripts\agents-live.exe'
    & $executable --version
    if ($LASTEXITCODE -ne 0) { throw "Installed command failed: $executable" }
    $commandRoot = Split-Path -Parent $executable
    $pathEntries = @($env:Path -split ';' | Where-Object { $_ })
    $retained = @($pathEntries | Where-Object {
        -not [string]::Equals($_, $commandRoot, [StringComparison]::OrdinalIgnoreCase)
    })
    $env:Path = (@($commandRoot) + $retained) -join ';'
    Write-Output "Agents Live is ready: $executable"
} finally {
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
}
