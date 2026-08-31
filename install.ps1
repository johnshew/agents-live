[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Version
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

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
    $matches = @($Release.assets | Where-Object { $_.name -eq $Name })
    if ($matches.Count -ne 1) {
        throw "Release v$ResolvedVersion does not contain exactly one $Name asset."
    }
    $asset = $matches[0]
    $expectedUrl = "$downloadRoot/v$ResolvedVersion/$Name"
    if ($asset.state -ne 'uploaded' -or $asset.browser_download_url -ne $expectedUrl -or
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

if ($Version -and $Version -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') {
    throw "'$Version' is not an exact stable release version."
}
$metadataUrl = if ($Version) { "$apiRoot/tags/v$Version" } else { "$apiRoot/latest" }
$release = Get-ReleaseJson $metadataUrl
$resolved = [string]$release.tag_name
if ($resolved -notmatch '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or
    $release.draft -ne $false -or $release.prerelease -ne $false) {
    throw "GitHub release metadata does not identify a stable release."
}
$resolved = $resolved.Substring(1)
if ($Version -and $resolved -ne $Version) {
    throw "GitHub returned release $resolved, expected exactly $Version."
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
    $versions = Join-Path $installRoot 'versions'
    $target = Join-Path $versions $resolved
    if (-not (Test-Path -LiteralPath $target)) {
        $staging = Join-Path $versions ".staging-$resolved"
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Path $versions -Force | Out-Null
        & $uvCommand.Source venv --relocatable --python 3.12 $staging
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the dedicated Agents Live environment.' }
        $python = Join-Path $staging 'Scripts\python.exe'
        & $uvCommand.Source pip install --python $python --reinstall-package agents-live $wheel
        if ($LASTEXITCODE -ne 0) { throw 'Could not install Agents Live into its dedicated environment.' }
        & $python -I -c "from agents_live import __version__; assert __version__ == '$resolved'"
        if ($LASTEXITCODE -ne 0) { throw 'The dedicated environment reported the wrong version.' }
        Move-Item -LiteralPath $staging -Destination $target
    }

    $env:AGENTS_LIVE_BOOTSTRAP_WHEEL = $wheel
    $env:AGENTS_LIVE_BOOTSTRAP_WHEEL_SHA256 = $wheelAsset.digest.Substring(7)
    $env:AGENTS_LIVE_BOOTSTRAP_MIGRATE_UV = '1'
    $executable = Join-Path $target 'Scripts\agents-live.exe'
    & $executable install-release $resolved --install-root $installRoot --activate
    if ($LASTEXITCODE -ne 0) { throw "Agents Live installation failed with exit code $LASTEXITCODE." }
    $executable = Join-Path $installRoot 'current\Scripts\agents-live.exe'
    & $executable --version
    if ($LASTEXITCODE -ne 0) { throw "Installed command failed: $executable" }
    Write-Output "Agents Live is ready: $executable"
} finally {
    Remove-Item Env:AGENTS_LIVE_BOOTSTRAP_WHEEL -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTS_LIVE_BOOTSTRAP_WHEEL_SHA256 -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTS_LIVE_BOOTSTRAP_MIGRATE_UV -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
}