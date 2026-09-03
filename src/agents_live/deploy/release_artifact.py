"""Resolve and authenticate wheels from the official GitHub release channel."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from . import layout

API_ROOT = "https://api.github.com/repos/johnshew/agents-live/releases"
DOWNLOAD_ROOT = "https://github.com/johnshew/agents-live/releases/download"
NETWORK_TIMEOUT = 30
MAX_METADATA_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
_STABLE_VERSION = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
_RELEASE_VERSION = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:(?:a|b|rc)\d+|\.dev\d+)?(?:\+[0-9A-Za-z]+(?:[._-][0-9A-Za-z]+)*)?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class ReleaseArtifactError(RuntimeError):
    """Official release metadata or artifact bytes failed closed."""


@dataclass(frozen=True)
class ReleaseArtifact:
    version: str
    name: str
    url: str
    sha256: str
    size: int


def _open(
    url: str,
    *,
    opener: Callable[..., Any],
) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "agents-live-bootstrap",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        return opener(request, timeout=NETWORK_TIMEOUT)
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseArtifactError(
            f"could not download {url}: {exc}; no package-index fallback was used"
        ) from exc


def _trusted_response(response: Any, *, metadata: bool) -> None:
    final_url = response.geturl()
    parsed = urlparse(final_url)
    hosts = {"api.github.com"} if metadata else _DOWNLOAD_HOSTS
    if parsed.scheme != "https" or parsed.hostname not in hosts:
        raise ReleaseArtifactError(
            f"GitHub redirected to an untrusted URL: {final_url}")


def _read_metadata(url: str, *, opener: Callable[..., Any]) -> dict[str, Any]:
    with _open(url, opener=opener) as response:
        _trusted_response(response, metadata=True)
        content = response.read(MAX_METADATA_BYTES + 1)
    if len(content) > MAX_METADATA_BYTES:
        raise ReleaseArtifactError("GitHub release metadata exceeded 1 MiB")
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseArtifactError(
            "GitHub returned invalid release metadata") from exc
    if not isinstance(document, dict):
        raise ReleaseArtifactError("GitHub returned invalid release metadata")
    return document


def _release(version: str | None, *, opener: Callable[..., Any]
             ) -> tuple[str, dict[str, Any]]:
    if version is None:
        metadata_url = f"{API_ROOT}/latest"
    else:
        version = layout.generation_name(version)
        if _RELEASE_VERSION.fullmatch(version) is None:
            raise ReleaseArtifactError(
                f"'{version}' is not an exact release version")
        metadata_url = f"{API_ROOT}/tags/v{quote(version, safe='')}"

    document = _read_metadata(metadata_url, opener=opener)
    tag = document.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ReleaseArtifactError("GitHub release metadata has no version tag")
    resolved = tag[1:]
    if _RELEASE_VERSION.fullmatch(resolved) is None:
        raise ReleaseArtifactError(
            f"GitHub release tag {tag!r} is not a release version")
    if version is not None and resolved != version:
        raise ReleaseArtifactError(
            f"GitHub returned release {resolved}, expected exactly {version}")
    prerelease = _STABLE_VERSION.fullmatch(resolved) is None
    if (
        document.get("draft") is not False
        or document.get("prerelease") is not prerelease
        or version is None and prerelease
    ):
        expected = "prerelease" if prerelease else "stable"
        raise ReleaseArtifactError(
            f"GitHub release v{resolved} is not a published {expected} release")

    return resolved, document


def _asset(resolved: str, document: dict[str, Any], name: str
           ) -> ReleaseArtifact:
    if not name or Path(name).name != name:
        raise ReleaseArtifactError(f"invalid release asset name: {name!r}")
    assets = document.get("assets")
    if not isinstance(assets, list):
        raise ReleaseArtifactError(
            f"GitHub release v{resolved} has no artifact metadata")
    matches = [
        asset for asset in assets
        if isinstance(asset, dict) and asset.get("name") == name
    ]
    if len(matches) != 1:
        raise ReleaseArtifactError(
            f"GitHub release v{resolved} does not contain exactly one {name}")
    asset = matches[0]
    expected_url = f"{DOWNLOAD_ROOT}/v{resolved}/{name}"
    digest = asset.get("digest")
    size = asset.get("size")
    if (
        asset.get("state") != "uploaded"
        or asset.get("browser_download_url") != expected_url
        or not isinstance(digest, str)
        or not digest.startswith("sha256:")
        or _SHA256.fullmatch(digest.removeprefix("sha256:")) is None
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= MAX_ARTIFACT_BYTES
    ):
        raise ReleaseArtifactError(
            f"GitHub release v{resolved} has incomplete or invalid provenance "
            f"for {name}")
    return ReleaseArtifact(
        resolved, name, expected_url, digest.removeprefix("sha256:"), size)


def resolve_asset(
    name: str,
    version: str | None = None,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> ReleaseArtifact:
    """Resolve one named release asset and its GitHub-recorded digest."""
    resolved, document = _release(version, opener=opener)
    return _asset(resolved, document, name)


def resolve(
    version: str | None = None,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> ReleaseArtifact:
    """Resolve one stable or explicitly requested prerelease wheel."""
    resolved, document = _release(version, opener=opener)
    return _asset(
        resolved, document, f"agents_live-{resolved}-py3-none-any.whl")


def verify_file(artifact: ReleaseArtifact, path: Path) -> None:
    """Fail unless a local file is exactly the authenticated release asset."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ReleaseArtifactError(
            f"could not read {artifact.name}: {exc}") from exc
    if size != artifact.size:
        raise ReleaseArtifactError(
            f"{artifact.name} was {size} bytes, expected {artifact.size}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual != artifact.sha256:
        raise ReleaseArtifactError(
            f"{artifact.name} checksum mismatch: expected "
            f"{artifact.sha256}, got {actual}")


@contextmanager
def verified_download(
    artifact: ReleaseArtifact,
    *,
    root: Path | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Iterator[Path]:
    """Yield exact verified wheel bytes, removing the staging area afterward."""
    install_root = root or layout.installation_root()
    install_root.mkdir(parents=True, exist_ok=True)
    staging = install_root / f".artifact-{uuid.uuid4().hex}"
    staging.mkdir()
    destination = staging / artifact.name
    digest = hashlib.sha256()
    size = 0
    try:
        with _open(artifact.url, opener=opener) as response:
            _trusted_response(response, metadata=False)
            with destination.open("xb") as stream:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_ARTIFACT_BYTES or size > artifact.size:
                        raise ReleaseArtifactError(
                            f"{artifact.name} exceeded its recorded size")
                    digest.update(block)
                    stream.write(block)
        verify_file(artifact, destination)
        yield destination
    finally:
        shutil.rmtree(staging, ignore_errors=True)
