#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# ///
"""Prepare and publish an agents-live release from a clean main branch."""
from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
VERSION_FILES = (
    ROOT / "src" / "agents_live" / "__init__.py",
    ROOT / "src" / "agents_live" / "cli.py",
    ROOT / "src" / "agents_live" / "skill" / "VERSION",
)
CHANGELOG = ROOT / "src" / "agents_live" / "skill" / "docs" / "changelog.md"
RELEASE_FILES = (PYPROJECT, *VERSION_FILES, CHANGELOG)
VERSION_RE = re.compile(r'^version = "(\d+\.\d+\.\d+)"$', re.MULTILINE)
BUMP_ORDER = {"patch": 0, "minor": 1, "major": 2}


class ReleaseError(RuntimeError):
    """A release precondition or operation failed."""


def _run(argv: list[str], *, capture: bool = False) -> str:
    print(f"+ {shlex.join(argv)}", flush=True)
    result = subprocess.run(
        argv,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def _git(*args: str) -> str:
    return _run(["git", *args], capture=True)


def _current_version() -> str:
    match = VERSION_RE.search(PYPROJECT.read_text(encoding="utf-8"))
    if match is None:
        raise ReleaseError("cannot read a stable X.Y.Z version from pyproject.toml")
    return match.group(1)


def _next_version(current: str, bump: str) -> str:
    major, minor, patch = (int(part) for part in current.split("."))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _replace_once(path: Path, old: str, new: str) -> None:
    content = path.read_text(encoding="utf-8")
    if content.count(old) != 1:
        raise ReleaseError(
            f"expected one {old!r} occurrence in {path.relative_to(ROOT)}"
        )
    path.write_text(content.replace(old, new), encoding="utf-8")


def _unreleased_notes(changelog: str | None = None) -> str:
    content = (
        CHANGELOG.read_text(encoding="utf-8")
        if changelog is None
        else changelog
    )
    marker = "## Unreleased\n\n"
    if content.count(marker) != 1:
        raise ReleaseError("changelog must contain one empty Unreleased heading marker")
    notes = re.split(r"(?m)^## ", content.split(marker, 1)[1], maxsplit=1)[0].strip()
    if not notes:
        raise ReleaseError("changelog Unreleased section has no release notes")
    return notes


def _version_notes(version: str) -> str:
    content = CHANGELOG.read_text(encoding="utf-8")
    heading = re.compile(rf"(?m)^## {re.escape(version)} - \d{{4}}-\d{{2}}-\d{{2}}\n")
    match = heading.search(content)
    if match is None:
        raise ReleaseError(f"changelog has no section for {version}")
    notes = re.split(r"(?m)^## ", content[match.end():], maxsplit=1)[0].strip()
    if not notes:
        raise ReleaseError(f"changelog section for {version} is empty")
    return notes


def _minimum_bump(notes: str) -> str:
    if re.search(r"(?mi)^-\s+\w+(?:\([^)]*\))?!:|BREAKING CHANGE:", notes):
        return "major"
    if re.search(r"(?mi)^-\s+feat(?:\([^)]*\))?:", notes):
        return "minor"
    return "patch"


def _check_bump(bump: str) -> str:
    minimum = _minimum_bump(_unreleased_notes())
    if BUMP_ORDER[bump] < BUMP_ORDER[minimum]:
        raise ReleaseError(
            f"changelog requires at least a {minimum} bump; "
            f"rerun with --bump {minimum}"
        )
    return minimum


def _update_versions(current: str, target: str) -> None:
    changelog = CHANGELOG.read_text(encoding="utf-8")
    _unreleased_notes(changelog)
    _run(["uv", "version", target, "--no-sync"])
    _replace_once(
        VERSION_FILES[0],
        f'__version__ = "{current}"',
        f'__version__ = "{target}"',
    )
    _replace_once(VERSION_FILES[1], f"/blob/v{current}/", f"/blob/v{target}/")
    _replace_once(VERSION_FILES[2], f"{current}\n", f"{target}\n")

    marker = "## Unreleased\n\n"
    release_heading = f"{marker}## {target} - {date.today().isoformat()}\n\n"
    CHANGELOG.write_text(
        changelog.replace(marker, release_heading), encoding="utf-8"
    )


def _require_tools() -> None:
    missing = [name for name in ("git", "gh", "uv") if shutil.which(name) is None]
    if missing:
        raise ReleaseError(f"missing required commands: {', '.join(missing)}")


def _check_prepare_state(target: str, *, fetch: bool) -> None:
    if _git("status", "--porcelain"):
        raise ReleaseError("working tree must be clean")
    if _git("branch", "--show-current") != "main":
        raise ReleaseError("releases must run from main")
    if fetch:
        _run(["git", "fetch", "--quiet", "origin", "main", "--tags"])
    if _git("rev-parse", "HEAD") != _git("rev-parse", "origin/main"):
        raise ReleaseError("main must match origin/main before release")
    tag = f"v{target}"
    local_tag = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"],
        cwd=ROOT,
    )
    if local_tag.returncode == 0:
        raise ReleaseError(f"tag {tag} already exists")


def _check_publish_state(version: str) -> bool:
    """Validate a prepared release and return whether it still needs pushing."""
    if _git("status", "--porcelain"):
        raise ReleaseError("working tree must be clean")
    if _git("branch", "--show-current") != "main":
        raise ReleaseError("releases must run from main")
    _run(["git", "fetch", "--quiet", "origin", "main", "--tags"])
    head = _git("rev-parse", "HEAD")
    origin = _git("rev-parse", "origin/main")
    needs_push = head != origin
    if needs_push:
        if _git("rev-list", "--count", "origin/main..HEAD") != "1":
            raise ReleaseError(
                "prepared main must be exactly one commit ahead of origin/main")
        if _git("merge-base", "HEAD", "origin/main") != origin:
            raise ReleaseError("prepared main must be based directly on origin/main")
    tag = f"v{version}"
    try:
        if _git("cat-file", "-t", tag) != "tag":
            raise ReleaseError(f"tag {tag} must be annotated")
        tag_commit = _git("rev-parse", f"{tag}^{{}}")
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(f"annotated tag {tag} is missing") from exc
    if tag_commit != head:
        raise ReleaseError(f"tag {tag} must point to HEAD")
    expected = {str(path.relative_to(ROOT)) for path in RELEASE_FILES}
    changed = set(_git("diff", "--name-only", "HEAD^..HEAD").splitlines())
    if changed != expected:
        raise ReleaseError(
            "prepared commit has an unexpected file set: "
            f"expected {sorted(expected)}, got {sorted(changed)}"
        )
    return needs_push


def _check_release_diff() -> None:
    changed = set(_git("diff", "--name-only").splitlines())
    expected = {str(path.relative_to(ROOT)) for path in RELEASE_FILES}
    if changed != expected:
        raise ReleaseError(
            "version bump changed an unexpected file set: "
            f"expected {sorted(expected)}, got {sorted(changed)}"
        )
    _run(["git", "diff", "--check"])


def _print_plan(current: str, target: str, minimum_bump: str) -> None:
    tag = f"v{target}"
    print(f"Release plan: {current} -> {target}")
    print(f"Minimum bump from changelog: {minimum_bump}")
    print("Version files:")
    for path in RELEASE_FILES:
        print(f"  {path.relative_to(ROOT)}")
    print("Commands:")
    commands = (
        "uv run --script tools/pre-release-audit.py",
        "uv run --with-editable . --script tests/test_smoke.py",
        "uv build",
        f"git commit -m 'chore(build): bump version to {tag}' ...",
        f"git tag -a {tag}",
        f"git push --atomic origin main {tag}",
        f"gh release create {tag} --verify-tag --generate-notes "
        "--notes-file <changelog section>",
    )
    for command in commands:
        print(f"  {command}")


def preview(bump: str) -> None:
    current = _current_version()
    target = _next_version(current, bump)
    minimum_bump = _check_bump(bump)
    _print_plan(current, target, minimum_bump)


def prepare(bump: str) -> None:
    _require_tools()
    current = _current_version()
    target = _next_version(current, bump)
    minimum_bump = _check_bump(bump)
    _print_plan(current, target, minimum_bump)
    _check_prepare_state(target, fetch=True)
    original = {path: path.read_bytes() for path in RELEASE_FILES}
    original_head = _git("rev-parse", "HEAD")
    committed = False
    try:
        _update_versions(current, target)
        _check_release_diff()
        _run(["uv", "run", "--script", "tools/pre-release-audit.py"])
        _run(["uv", "run", "--with-editable", ".", "--script", "tests/test_smoke.py"])
        _run(["uv", "build"])
        _run(["git", "add", *[str(path.relative_to(ROOT)) for path in RELEASE_FILES]])
        message = f"chore(build): bump version to v{target}"
        footer = "\U0001f4e6 - Generated by Copilot"
        _run(["git", "commit", "-m", message, "-m", footer])
        committed = True
    except BaseException:
        committed = _git("rev-parse", "HEAD") != original_head
        if not committed:
            subprocess.run(
                ["git", "reset", "--quiet", "HEAD", "--",
                 *[str(path.relative_to(ROOT)) for path in RELEASE_FILES]],
                cwd=ROOT,
                check=False,
            )
            for path, content in original.items():
                path.write_bytes(content)
            print("Restored release files after the failed preparation.", file=sys.stderr)
        raise

    tag = f"v{target}"
    _run(["git", "tag", "-a", tag, "-m", f"agents-live {tag}"])
    print(f"Prepared {tag}. Inspect dist/ and the commit, then run:")
    print("  uv run --script tools/release.py --publish --yes")


def publish() -> None:
    _require_tools()
    version = _current_version()
    needs_push = _check_publish_state(version)
    tag = f"v{version}"
    existing = subprocess.run(
        ["gh", "release", "view", tag, "--json", "url"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        print(f"GitHub release {tag} already exists: {existing.stdout.strip()}")
        return
    notes = _version_notes(version)
    _run(["uv", "run", "--script", "tools/pre-release-audit.py"])
    _run(["uv", "run", "--with-editable", ".", "--script", "tests/test_smoke.py"])
    _run(["uv", "build"])
    if needs_push:
        _run(["git", "push", "--atomic", "origin", "main", tag])
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".md", delete_on_close=False
    ) as notes_file:
        notes_file.write(notes + "\n")
        notes_file.close()
        # gh prepends the file's notes to the --generate-notes body, so the
        # release shows the curated changelog first, then the PR list.
        _run([
            "gh", "release", "create", tag,
            "--verify-tag", "--generate-notes",
            "--notes-file", notes_file.name,
            "--title", f"agents-live {tag}",
        ])
    print(f"Published GitHub release {tag}; the PyPI workflow is now running.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bump",
        choices=("patch", "minor", "major"),
        default="patch",
        help="Semantic version component to bump (default: patch)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the release plan without changing files or remotes",
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Bump, verify, build, commit, and tag locally",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Verify and publish a prepared release",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm commit, tag, push, and GitHub release creation",
    )
    args = parser.parse_args(argv)
    selected = sum((args.dry_run, args.prepare, args.publish))
    if selected != 1:
        parser.error("choose exactly one of --dry-run, --prepare, or --publish")
    if (args.prepare or args.publish) and not args.yes:
        parser.error("--prepare and --publish require --yes")
    if args.publish and args.bump != "patch":
        parser.error("--bump applies to --dry-run and --prepare only")
    try:
        if args.dry_run:
            preview(args.bump)
        elif args.prepare:
            prepare(args.bump)
        else:
            publish()
    except KeyboardInterrupt:
        print("release interrupted", file=sys.stderr)
        return 130
    except (OSError, ReleaseError, subprocess.CalledProcessError) as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())