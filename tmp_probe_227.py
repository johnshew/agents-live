"""Throwaway driver: does prepare revalidate release files after the gates?

Runs against an isolated clone. A stand-in gate restores the changelog
mid-run, which is the v5.4.1 incident described in issue #227.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest import mock

clone = Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location(
    "release_under_test", clone / "tools" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

changelog = release.CHANGELOG
pristine_changelog = changelog.read_text(encoding="utf-8")


def bump_files(current: str, target: str) -> None:
    """Stand-in for _update_versions: touches exactly the release files."""
    for path in release.VERSION_FILES:
        path.write_text(
            path.read_text(encoding="utf-8").replace(current, target),
            encoding="utf-8")
    release.PYPROJECT.write_text(
        release.PYPROJECT.read_text(encoding="utf-8").replace(
            f'version = "{current}"', f'version = "{target}"'),
        encoding="utf-8")
    changelog.write_text(
        pristine_changelog.replace("## Unreleased", f"## {target} - 2026-08-09"),
        encoding="utf-8")


backup = clone / "pristine-changelog.txt"
backup.write_text(pristine_changelog, encoding="utf-8")
restore_gate = [
    sys.executable, "-c",
    "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text("
    "pathlib.Path(sys.argv[2]).read_text(encoding='utf-8'),encoding='utf-8');"
    "print('gate restored the changelog to its pre-bump text')",
    str(changelog), str(backup),
]

with (
    mock.patch.object(release, "_require_tools"),
    mock.patch.object(release, "_check_prepare_state"),
    mock.patch.object(release, "_update_versions", side_effect=bump_files),
    mock.patch.object(release, "_gate_commands", return_value=[restore_gate]),
):
    try:
        release.prepare("major")
    except BaseException as exc:  # noqa: BLE001
        print(f"prepare raised: {type(exc).__name__}: {exc}")
    else:
        print(">>> prepare reported SUCCESS")

files = subprocess.run(
    ["git", "show", "--name-only", "--format=", "HEAD"],
    cwd=clone, capture_output=True, text=True, check=False).stdout.split()
subject = subprocess.run(
    ["git", "log", "-1", "--format=%s"],
    cwd=clone, capture_output=True, text=True, check=False).stdout.strip()
tags = subprocess.run(
    ["git", "tag"], cwd=clone, capture_output=True, text=True, check=False).stdout.split()
print("commit subject :", subject)
print("committed files:", files)
print("tags created   :", tags)
print()
print("FILE COUNT:", len(files), "(a correct release commit has 4)")
print("CHANGELOG IN COMMIT:", any("changelog" in name for name in files))
