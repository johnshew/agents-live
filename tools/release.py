#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# ///
"""Prepare and publish an agents-live release from a clean main branch."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import date
from pathlib import Path
from typing import NamedTuple


ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
VERSION_FILES = (
    ROOT / "src" / "agents_live" / "__init__.py",
    ROOT / "src" / "agents_live" / "skill" / "VERSION",
)
CHANGELOG = ROOT / "src" / "agents_live" / "skill" / "docs" / "changelog.md"
REPO_OWNER = "johnshew"
REPO_NAME = "agents-live"
CHANGELOG_URL = (
    "https://github.com/johnshew/agents-live/blob/{tag}/"
    "src/agents_live/skill/docs/changelog.md"
)
RELEASE_FILES = (PYPROJECT, *VERSION_FILES, CHANGELOG)
VERSION_RE = re.compile(r'^version = "(\d+\.\d+\.\d+)"$', re.MULTILINE)
BUMP_ORDER = {"patch": 0, "minor": 1, "major": 2}
COMPARE_URL = "https://github.com/johnshew/agents-live/compare/{base}...{tag}"
SUMMARY_END_RE = re.compile(r"[.!?](?: \(#\d+(?:, #\d+)*\))?$")
ISSUE_REFS_RE = re.compile(r"\s*\((#\d+(?:,\s*#\d+)*)\)\s*$")
COMMIT_TYPE_RE = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<bang>!)?: ")
MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) ")
BREAKING_RE = re.compile(r"(?m)^\s*BREAKING CHANGE:\s*")
# Rows are ordered by what the change is, breaking first; anything with an
# unrecognised prefix sorts last rather than failing the release.
TYPE_ORDER = ("feat", "fix", "perf", "refactor", "docs", "test", "build", "chore")


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
        # Pin the gate commands to this checkout: the repository root
        # carries no project marker, so an unpinned `agents-live`
        # invocation would fall through to the user-level registry
        # default and run against an unrelated repository (#85).
        env={**os.environ, "AGENTS_LIVE_REPO": str(ROOT)},
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


class _Entry(NamedTuple):
    """One changelog bullet, ready to render as a release-note row."""

    summary: str
    kind: str
    breaking: bool
    issues: tuple[int, ...]
    migration: str


def _issue_refs(line: str) -> tuple[int, ...]:
    match = ISSUE_REFS_RE.search(line)
    if match is None:
        return ()
    return tuple(int(ref.lstrip("#")) for ref in match.group(1).split(", "))


def _summary_text(line: str) -> str:
    """Strip the bullet marker, trailing issue refs, and the sentence period.

    The changelog needs a standalone sentence; a release-note row reads
    better with the annotation carrying the references instead.
    """
    text = ISSUE_REFS_RE.sub("", line[2:]).rstrip()
    return text[:-1] if text.endswith(".") else text


def _changelog_entries(notes: str, section: str) -> list[_Entry]:
    blocks: list[list[str]] = []
    for line in notes.splitlines():
        if line.startswith("- "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    if not blocks:
        raise ReleaseError(f"changelog section {section} has no bullet entries")

    entries: list[_Entry] = []
    for block in blocks:
        summary = block[0]
        if not SUMMARY_END_RE.search(summary):
            raise ReleaseError(
                f"changelog section {section} has an incomplete first-line summary: "
                f"{summary!r}; end the standalone sentence with punctuation"
            )
        prefix = COMMIT_TYPE_RE.match(summary[2:])
        body = "\n".join(line[2:] if line.startswith("  ") else line
                         for line in block[1:])
        split = BREAKING_RE.split(body, maxsplit=1)
        entries.append(_Entry(
            summary=_summary_text(summary),
            kind=prefix.group("type") if prefix else "",
            breaking=bool(prefix and prefix.group("bang")),
            issues=_issue_refs(summary),
            migration=split[1].strip() if len(split) > 1 else "",
        ))
    return entries


def _entry_rank(entry: _Entry) -> int:
    if entry.breaking or entry.migration:
        return 0
    if entry.kind in TYPE_ORDER:
        return 1 + TYPE_ORDER.index(entry.kind)
    return 1 + len(TYPE_ORDER)


def _reflow(text: str) -> str:
    """Rewrap prose lifted out of the changelog's own wrapping.

    Hyphen and long-word breaking stay off: these paragraphs carry inline
    code such as `--transfer-here`, and a wrap inside one renders as a
    command with a space in it.
    """
    return "\n\n".join(
        textwrap.fill(
            " ".join(paragraph.split()),
            width=78,
            break_on_hyphens=False,
            break_long_words=False,
        )
        for paragraph in text.split("\n\n")
        if paragraph.strip()
    )


def _previous_tag(tag: str) -> str:
    """The release this one follows, or empty when it is the first."""
    try:
        return _git("describe", "--tags", "--abbrev=0", f"{tag}^")
    except subprocess.CalledProcessError:
        return ""


def _merged_pulls(base: str, tag: str) -> dict[int, tuple[str, tuple[int, ...]]]:
    """Map each pull request merged in the range to its title and closed issues.

    The closing issues are only exposed through GraphQL; `gh pr view --json`
    has no such field. Best effort by design: a lookup that fails leaves the
    pull requests unannotated rather than blocking a release that is
    otherwise ready.
    """
    if not base:
        return {}
    subjects = _git("log", "--merges", "--format=%s", f"{base}..{tag}").splitlines()
    numbers = sorted({
        int(match.group(1))
        for match in (MERGE_PR_RE.match(subject) for subject in subjects)
        if match is not None
    })
    if not numbers:
        return {}
    aliases = " ".join(
        f"p{number}: pullRequest(number: {number}) {{ ...pull }}"
        for number in numbers
    )
    query = (
        "query($owner: String!, $name: String!) { "
        f"repository(owner: $owner, name: $name) {{ {aliases} }} }} "
        "fragment pull on PullRequest { number title "
        "closingIssuesReferences(first: 20) { nodes { number } } }"
    )
    try:
        raw = _run(
            ["gh", "api", "graphql", "-f", f"query={query}",
             "-F", f"owner={REPO_OWNER}", "-F", f"name={REPO_NAME}"],
            capture=True,
        )
        repository = json.loads(raw)["data"]["repository"]
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError):
        print(
            f"Warning: could not read pull requests {numbers}; "
            "the notes will carry changelog entries only.",
            file=sys.stderr,
        )
        return {}
    pulls: dict[int, tuple[str, tuple[int, ...]]] = {}
    for number in numbers:
        pull = repository.get(f"p{number}")
        if not pull:
            continue
        closes = tuple(
            node["number"]
            for node in pull["closingIssuesReferences"]["nodes"]
        )
        pulls[number] = (pull["title"], closes)
    return pulls


def _normalize_title(text: str) -> str:
    return " ".join(text.split()).rstrip(".").casefold()


def _annotate(pull_numbers: list[int], issues: tuple[int, ...]) -> str:
    """Render the reference suffix.

    GitHub autolinks issues and pull requests identically, so the kind is
    spelled out rather than left to the reader to infer from position.
    """
    parts: list[str] = []
    if pull_numbers:
        parts.append("PR " + ", ".join(f"#{number}" for number in pull_numbers))
    if issues:
        refs = ", ".join(f"#{number}" for number in issues)
        parts.append(f"fixes {refs}" if pull_numbers else f"closes {refs}")
    return f" ({' '.join(parts)})" if parts else ""


def _release_notes(version: str) -> str:
    """Build the whole release body from the changelog and the merged pulls."""
    tag = f"v{version}"
    entries = _changelog_entries(_version_notes(version), version)
    base = _previous_tag(tag)
    pulls = _merged_pulls(base, tag)

    rows: list[tuple[int, str]] = []
    actions: list[str] = []
    claimed: set[int] = set()
    for entry in sorted(entries, key=_entry_rank):
        matched = sorted(
            number for number, (_, closes) in pulls.items()
            if set(closes) & set(entry.issues)
        )
        claimed.update(matched)
        annotation = _annotate(matched, entry.issues)
        rows.append((_entry_rank(entry), f"- {entry.summary}{annotation}"))
        if entry.migration:
            # The changelog runs the migration on from "BREAKING CHANGE:";
            # lifted out on its own it has to read as a sentence, and it
            # keeps the wrap of the sentence it was cut from until reflowed.
            migration = entry.migration[0].upper() + entry.migration[1:]
            actions.append(_reflow(f"{migration}{annotation}"))

    # A pull request the changelog missed still gets a row, so issue-tracked
    # work cannot go unmentioned. One that closes no issue is either a step
    # in curated work or a change the changelog deliberately passed over -
    # adding it duplicates rows whenever a release is organised around an
    # umbrella issue - so it is named on stderr instead.
    summaries = {_normalize_title(entry.summary) for entry in entries}
    for number in sorted(set(pulls) - claimed):
        title, closes = pulls[number]
        if _normalize_title(title) in summaries:
            continue
        if not closes:
            print(
                f"Note: pull request #{number} ({title}) has no changelog entry "
                "and closes no issue; it is left out of the notes.",
                file=sys.stderr,
            )
            continue
        print(
            f"Warning: pull request #{number} has no changelog entry; "
            "its title is used verbatim.",
            file=sys.stderr,
        )
        prefix = COMMIT_TYPE_RE.match(title)
        kind = prefix.group("type") if prefix else ""
        rank = 1 + (TYPE_ORDER.index(kind) if kind in TYPE_ORDER else len(TYPE_ORDER))
        if prefix and prefix.group("bang"):
            rank = 0
        rows.append((rank, f"- {title}{_annotate([number], closes)}"))

    sections: list[str] = []
    if actions:
        sections.append("## Action required\n\n" + "\n\n".join(actions))
    ordered = [row for _, row in sorted(rows, key=lambda row: row[0])]
    sections.append("## Changes\n\n" + "\n".join(ordered))
    links = f"[Full changelog]({CHANGELOG_URL.format(tag=tag)})"
    if base:
        compare = COMPARE_URL.format(base=base, tag=tag)
        links += f" | [{base}...{tag}]({compare})"
    sections.append(links)
    return "\n\n".join(sections)


def _write_release_notes(tag: str, notes: str, *, create: bool) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".md", delete_on_close=False
    ) as notes_file:
        notes_file.write(notes + "\n")
        notes_file.close()
        if create:
            _run([
                "gh", "release", "create", tag,
                "--verify-tag", "--notes-file", notes_file.name,
                "--title", f"agents-live {tag}",
            ])
        else:
            _run(["gh", "release", "edit", tag, "--notes-file", notes_file.name])


def notes(tag: str, *, apply: bool) -> None:
    """Regenerate the notes for an existing release, previewing by default."""
    _require_tools()
    _run(["git", "fetch", "--quiet", "origin", "main", "--tags"])
    body = _release_notes(tag.removeprefix("v"))
    if not apply:
        print()
        print(body)
        print()
        print(f"Preview only. Rerun with --yes to apply these notes to {tag}.")
        return
    current = _run(
        ["gh", "release", "view", tag, "--json", "body", "--jq", ".body"],
        capture=True,
    )
    if current.strip() == body.strip():
        print(f"Release {tag} already carries these notes.")
        return
    _write_release_notes(tag, body, create=False)
    print(f"Updated the notes on release {tag}.")


def _minimum_bump(notes: str) -> str:
    # BREAKING CHANGE is a footer, so it only counts at the start of a
    # line; unanchored, an entry that merely discusses one forces a major.
    if re.search(r"(?mi)^-\s+\w+(?:\([^)]*\))?!:|^\s*BREAKING CHANGE:", notes):
        return "major"
    if re.search(r"(?mi)^-\s+feat(?:\([^)]*\))?:", notes):
        return "minor"
    return "patch"


def _check_bump(bump: str) -> str:
    notes = _unreleased_notes()
    _changelog_entries(notes, "Unreleased")
    minimum = _minimum_bump(notes)
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
    _replace_once(VERSION_FILES[1], f"{current}\n", f"{target}\n")

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
    expected = {path.relative_to(ROOT).as_posix() for path in RELEASE_FILES}
    changed = set(_git("diff", "--name-only", "HEAD^..HEAD").splitlines())
    if changed != expected:
        raise ReleaseError(
            "prepared commit has an unexpected file set: "
            f"expected {sorted(expected)}, got {sorted(changed)}"
        )
    return needs_push


def _check_release_diff() -> None:
    changed = set(_git("diff", "--name-only").splitlines())
    expected = {path.relative_to(ROOT).as_posix() for path in RELEASE_FILES}
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
        "uv run --with-editable . agents-live --repo . smoketest",
        "uv build",
        f"git commit -m 'chore(build): bump version to {tag}' ...",
        f"git tag -a {tag}",
        f"git push --atomic origin main {tag}",
        f"gh release create {tag} --verify-tag "
        "--notes-file <changelog entries + merged pull requests>",
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
        # End-to-end gate: the framework smoketest exercises the real
        # trigger/run/status loop in this checkout, catching breaks the
        # unit suite cannot (e.g. module argv contract drift). --repo is
        # what makes "this checkout" true: without it the smoketest acts
        # on whatever root resolves, which on a host with a configured
        # default is some other project entirely.
        _run(["uv", "run", "--with-editable", ".", "agents-live",
              "--repo", str(ROOT), "smoketest"])
        _run(["uv", "build"])
        _run(["git", "add", *[str(path.relative_to(ROOT)) for path in RELEASE_FILES]])
        message = f"chore(build): bump version to v{target}"
        _run(["git", "commit", "-m", message])
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
        print(f"  Rerun the notes with: --notes {tag} --yes")
        return
    notes = _release_notes(version)
    _run(["uv", "run", "--script", "tools/pre-release-audit.py"])
    _run(["uv", "run", "--with-editable", ".", "--script", "tests/test_smoke.py"])
    _run(["uv", "run", "--with-editable", ".", "agents-live",
          "--repo", str(ROOT), "smoketest"])
    _run(["uv", "build"])
    if needs_push:
        _run(["git", "push", "--atomic", "origin", "main", tag])
    _write_release_notes(tag, notes, create=True)
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
        "--notes",
        metavar="TAG",
        help="Rebuild the notes on an already published release",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm commit, tag, push, and GitHub release creation",
    )
    args = parser.parse_args(argv)
    selected = sum((args.dry_run, args.prepare, args.publish, args.notes is not None))
    if selected != 1:
        parser.error(
            "choose exactly one of --dry-run, --prepare, --publish, or --notes")
    if (args.prepare or args.publish) and not args.yes:
        parser.error("--prepare and --publish require --yes")
    if (args.publish or args.notes) and args.bump != "patch":
        parser.error("--bump applies to --dry-run and --prepare only")
    try:
        if args.dry_run:
            preview(args.bump)
        elif args.prepare:
            prepare(args.bump)
        elif args.notes:
            notes(args.notes, apply=args.yes)
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