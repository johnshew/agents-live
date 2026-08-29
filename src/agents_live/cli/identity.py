"""User-visible identity for the installed runtime."""
from __future__ import annotations

import re


_RELEASE_VERSION = re.compile(r"\d+\.\d+\.\d+\Z")
_COMMIT = re.compile(r"(?:^|[.+])g([0-9a-f]+)(?:\Z|[.])", re.IGNORECASE)


def channel(version: str) -> str:
    """Classify stable releases and local development bake artifacts."""
    if _RELEASE_VERSION.fullmatch(version):
        return "release"
    if ".dev" in version:
        return "bake"
    return "unknown"


def commit(version: str) -> str | None:
    """Read a commit identifier from conventional local version metadata."""
    match = _COMMIT.search(version)
    return match.group(1) if match else None


def details(version: str) -> dict[str, str]:
    """Return the machine-readable installed runtime identity."""
    result = {"version": version, "channel": channel(version)}
    revision = commit(version)
    if revision:
        result["commit"] = revision
    return result


def label(version: str) -> str:
    """Render the version and its distribution channel."""
    fields = [f"channel: {channel(version)}"]
    revision = commit(version)
    if revision:
        fields.append(f"commit: {revision}")
    return f"agents-live {version} ({', '.join(fields)})"