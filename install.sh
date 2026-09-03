#!/bin/sh
set -eu

version=${1:-}
case "$version" in
  ""|[0-9]*.[0-9]*.[0-9]*) ;;
  *) echo "agents-live: '$version' is not an exact stable or prerelease version" >&2; exit 1 ;;
esac

temporary=$(mktemp -d "${TMPDIR:-/tmp}/agents-live-install.XXXXXX")
trap 'rm -rf "$temporary"' EXIT HUP INT TERM

if command -v uv >/dev/null 2>&1; then
  uv=$(command -v uv)
else
  uv_installer="$temporary/uv-install.sh"
  if ! curl --proto '=https' --tlsv1.2 -LsSf \
      https://astral.sh/uv/install.sh -o "$uv_installer"; then
    echo "agents-live: could not download uv; check proxy and TLS settings" >&2
    exit 1
  fi
  sh "$uv_installer"
  if command -v uv >/dev/null 2>&1; then
    uv=$(command -v uv)
  elif [ -x "$HOME/.local/bin/uv" ]; then
    uv="$HOME/.local/bin/uv"
  else
    echo "agents-live: uv installation completed but uv was not found" >&2
    exit 1
  fi
fi

cat >"$temporary/download.py" <<'PY'
import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

version, destination = sys.argv[1:]
api = os.environ.get(
    "AGENTS_LIVE_RELEASE_API",
    "https://api.github.com/repos/johnshew/agents-live/releases",
).rstrip("/")
download = os.environ.get(
    "AGENTS_LIVE_RELEASE_DOWNLOAD_ROOT",
    "https://github.com/johnshew/agents-live/releases/download",
).rstrip("/")
stable_version = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
release_version = re.compile(
  r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
  r"(?:(?:a|b|rc)\d+|\.dev\d+)?(?:\+[0-9A-Za-z]+(?:[._-][0-9A-Za-z]+)*)?")
if version and not release_version.fullmatch(version):
  raise SystemExit(
    f"agents-live: '{version}' is not an exact stable or prerelease version")
url = f"{api}/tags/v{version}" if version else f"{api}/latest"
request = urllib.request.Request(url, headers={
    "Accept": "application/vnd.github+json",
    "User-Agent": "agents-live-bootstrap",
    "X-GitHub-Api-Version": "2022-11-28",
})
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
    raise SystemExit(
        f"agents-live: could not retrieve release metadata; "
        f"check proxy and TLS settings: {error}")
tag = release.get("tag_name", "")
if (not tag.startswith("v")
    or not release_version.fullmatch(tag[1:])
    or release.get("draft") is not False):
  raise SystemExit("agents-live: release metadata is not published")
resolved = tag[1:]
if version and resolved != version:
    raise SystemExit(
        f"agents-live: GitHub returned {resolved}, expected exactly {version}")
expects_prerelease = bool(version and not stable_version.fullmatch(version))
if release.get("prerelease") is not expects_prerelease:
  raise SystemExit(
    f"agents-live: release v{resolved} has the wrong prerelease status")
name = f"agents_live-{resolved}-py3-none-any.whl"
matches = [asset for asset in release.get("assets", [])
           if asset.get("name") == name]
if len(matches) != 1:
    raise SystemExit(f"agents-live: release v{resolved} does not contain {name}")
asset = matches[0]
expected_url = f"{download}/v{resolved}/{name}"
digest = asset.get("digest", "")
size = asset.get("size")
if (asset.get("state") != "uploaded"
        or asset.get("browser_download_url") != expected_url
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        or not isinstance(size, int) or isinstance(size, bool) or size <= 0):
    raise SystemExit(
        f"agents-live: release v{resolved} has invalid provenance for {name}")
path = pathlib.Path(destination, name)
try:
    with urllib.request.urlopen(expected_url, timeout=30) as response, path.open("xb") as stream:
        value = response.read(size + 1)
        stream.write(value)
except (OSError, urllib.error.URLError) as error:
    raise SystemExit(
        f"agents-live: could not download {expected_url}; check proxy and TLS "
        f"settings; no package-index fallback was used: {error}")
actual = hashlib.sha256(value).hexdigest()
if len(value) != size or actual != digest[7:]:
    path.unlink(missing_ok=True)
    raise SystemExit(f"agents-live: authenticated download failed for {name}")
print(resolved)
print(path)
print(digest[7:])
PY

if ! result=$("$uv" run --no-project --python 3.12 \
    "$temporary/download.py" "$version" "$temporary"); then
  exit 1
fi
resolved=$(printf '%s\n' "$result" | sed -n '1p')
wheel=$(printf '%s\n' "$result" | sed -n '2p')
wheel_sha256=$(printf '%s\n' "$result" | sed -n '3p')
[ -n "$resolved" ] && [ -f "$wheel" ] || {
  echo "agents-live: verified release download returned no wheel" >&2
  exit 1
}

root=${AGENTS_LIVE_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/agents-live}
versions="$root/versions"
target="$versions/$resolved"
if [ ! -d "$target" ]; then
  staging="$versions/.staging-$resolved"
  rm -rf "$staging"
  mkdir -p "$versions"
  "$uv" venv --relocatable --python 3.12 "$staging"
  "$uv" pip install --python "$staging/bin/python3" \
    --reinstall-package agents-live "$wheel"
  "$staging/bin/python3" -I -c \
    "from agents_live import __version__; assert __version__ == '$resolved'"
  mv "$staging" "$target"
fi

AGENTS_LIVE_BOOTSTRAP_WHEEL="$wheel" \
AGENTS_LIVE_BOOTSTRAP_WHEEL_SHA256="$wheel_sha256" \
AGENTS_LIVE_BOOTSTRAP_MIGRATE_UV=1 \
  "$target/bin/agents-live" install-release "$resolved" \
    --install-root "$root" --activate

bin="$root/current/bin"
PATH="$bin:$PATH"
export PATH
"$bin/agents-live" --version
printf 'Agents Live is ready: %s\n' "$bin/agents-live"