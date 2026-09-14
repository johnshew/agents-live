#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# ///
"""Prepare and publish an agents-live release from a clean main branch."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
from datetime import date, datetime, timezone
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
BOOTSTRAP_ASSETS = (
    "install.ps1",
    "install.sh",
)
BOOTSTRAP_BUILD_INPUTS = (
    ROOT / "install.ps1",
    ROOT / "install.sh",
)
BOOTSTRAP_VERSION_MARKERS = {
    "install.ps1": "$embeddedVersion = ''",
    "install.sh": 'embedded_version=""',
}
VERSION_RE = re.compile(r'^version = "(\d+\.\d+\.\d+(?:rc[1-9]\d*)?)"$', re.MULTILINE)
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
ACCEPTANCE_SCHEMA = 2
PREPARATION_SCHEMA = 2
CHECKPOINT_SCHEMA = 1
ACTIVE_ATTEMPT: dict | None = None


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
                f"Note: pull request #{number} ({title}) could not be associated "
                "with a changelog entry and closes no issue; it is left out "
                "of the notes.",
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
    download_root = (
        f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/download/{tag}")
    sections.append(textwrap.dedent(f"""\
        ## Quick install

        Linux and WSL:

        ```bash
        curl -fsSL {download_root}/install.sh | sh
        ```

        Windows PowerShell:

        ```powershell
        irm {download_root}/install.ps1 | iex
        ```"""))
    ordered = [row for _, row in sorted(rows, key=lambda row: row[0])]
    sections.append("## Changes\n\n" + "\n".join(ordered))
    links = f"[Full changelog]({CHANGELOG_URL.format(tag=tag)})"
    if base:
        compare = COMPARE_URL.format(base=base, tag=tag)
        links += f" | [{base}...{tag}]({compare})"
    sections.append(links)
    return "\n\n".join(sections)


def _write_release_notes(
    tag: str, notes: str, *, create: bool, assets: tuple[Path, ...] = (),
    resume_draft: bool = False,
) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".md", delete_on_close=False
    ) as notes_file:
        notes_file.write(notes + "\n")
        notes_file.close()
        if create:
            if resume_draft:
                _run([
                    "gh", "release", "edit", tag,
                    "--notes-file", notes_file.name,
                    "--title", f"agents-live {tag}",
                ])
            else:
                _run([
                    "gh", "release", "create", tag,
                    "--verify-tag", "--draft",
                    "--notes-file", notes_file.name,
                    "--title", f"agents-live {tag}",
                ])
            if assets:
                if ACTIVE_ATTEMPT is not None:
                    metadata = json.loads(_run([
                        "gh", "release", "view", tag, "--json", "assets",
                    ], capture=True))
                    existing_assets = {item["name"]: item for item in metadata["assets"]}
                    if set(existing_assets) - {path.name for path in assets}:
                        raise ReleaseError("draft contains unexpected release assets")
                    for path in assets:
                        if path.name in existing_assets:
                            if existing_assets[path.name].get("digest") != f"sha256:{_sha256(path)}":
                                raise ReleaseError("draft asset differs from accepted bytes; never replace assets")
                        else:
                            _run(["gh", "release", "upload", tag, str(path)])
                else:
                    _run([
                        "gh", "release", "upload", tag,
                        *(str(path) for path in assets), "--clobber",
                    ])
            _run(["gh", "release", "edit", tag, "--draft=false"])
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


def _check_prepare_state(target: str, *, fetch: bool, resume: bool = False) -> None:
    if _git("status", "--porcelain"):
        raise ReleaseError("working tree must be clean")
    branch = _git("branch", "--show-current")
    candidate_branch = _candidate_branch(target)
    tag = f"v{target}"

    if branch.startswith("release/v") and branch.endswith("-candidate"):
        candidate_version = branch.removeprefix("release/v").removesuffix("-candidate")
        if not resume or candidate_version != target:
            raise ReleaseError(
                f"currently on candidate branch {branch}. "
                f"To resume post-commit preparation: uv run --script tools/release.py --prepare --resume --yes. "
                "To abandon this candidate, retain its branch, tag, and Git-local "
                "artifacts for inspection and prepare a different version from clean main."
            )

    if resume:
        if branch != candidate_branch:
            local_branch = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{candidate_branch}"],
                cwd=ROOT,
            )
            if local_branch.returncode != 0:
                raise ReleaseError(
                    f"candidate branch {candidate_branch} does not exist to resume; "
                    "run --prepare without --resume to create a new candidate"
                )
            _run(["git", "switch", candidate_branch])
            branch = candidate_branch
        if fetch:
            _run(["git", "fetch", "--quiet", "origin", "main", "--tags"])
        if _git("ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
            raise ReleaseError(f"tag {tag} is already remote; do not resume or overwrite a published candidate")
        if _current_version() != target:
            raise ReleaseError(f"candidate branch version does not match {target}")
        head = _git("rev-parse", "HEAD")
        origin = _git("rev-parse", "origin/main")
        if _git("rev-list", "--count", "origin/main..HEAD") != "1":
            raise ReleaseError(
                f"candidate branch {candidate_branch} must be exactly one commit ahead of origin/main"
            )
        if _git("merge-base", "HEAD", "origin/main") != origin:
            raise ReleaseError(
                f"candidate branch {candidate_branch} must be based directly on origin/main"
            )
        expected_msg = f"chore(build): bump version to {tag}"
        commit_msg = _git("log", "-1", "--format=%s")
        if commit_msg != expected_msg:
            raise ReleaseError(
                f"candidate commit message must be '{expected_msg}', got '{commit_msg}'"
            )
        local_tag = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"],
            cwd=ROOT,
        )
        if local_tag.returncode == 0:
            if _git("cat-file", "-t", tag) != "tag":
                raise ReleaseError(f"tag {tag} must be annotated")
            tag_commit = _git("rev-parse", f"{tag}^{{commit}}")
            if tag_commit != head:
                raise ReleaseError(
                    f"tag {tag} already exists but points to {tag_commit[:8]}, "
                    f"not candidate commit {head[:8]}"
                )
        return

    if branch != "main":
        raise ReleaseError("releases must run from main")
    if fetch:
        _run(["git", "fetch", "--quiet", "origin", "main", "--tags"])
    if _git("rev-parse", "HEAD") != _git("rev-parse", "origin/main"):
        raise ReleaseError("main must match origin/main before release")

    local_branch = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{candidate_branch}"],
        cwd=ROOT,
    )
    if local_branch.returncode == 0:
        raise ReleaseError(
            f"candidate branch {candidate_branch} already exists. "
            f"To resume post-commit preparation: uv run --script tools/release.py --prepare --resume --yes. "
            "Retain existing candidate evidence when preparing a different version."
        )
    local_tag = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"],
        cwd=ROOT,
    )
    if local_tag.returncode == 0:
        raise ReleaseError(
            f"tag {tag} already exists; do not delete or overwrite it. "
            "For a local candidate, switch to its candidate branch and use --prepare --resume --yes."
        )


def _stable_tag_version(tag: str) -> str:
    if re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag) is None:
        raise ReleaseError("publication requires a canonical stable vX.Y.Z tag")
    return tag[1:]


def verify_publication(tag: str) -> None:
    version = _stable_tag_version(tag)
    if _current_version() != version:
        raise ReleaseError("publication tag does not match the checked-out package")
    payload = json.loads(_run([
        "gh", "release", "view", tag, "--json", "tagName,isDraft,isPrerelease",
    ], capture=True))
    if (payload.get("tagName") != tag or payload.get("isDraft") is not False
            or payload.get("isPrerelease") is not False):
        raise ReleaseError("publication requires an existing non-draft stable release")
    if _git("rev-parse", f"{tag}^{{commit}}") != _git("rev-parse", "HEAD"):
        raise ReleaseError("publication checkout does not match the stable tag")


def verify_publication_assets(tag: str) -> None:
    verify_publication(tag)
    version = _stable_tag_version(tag)
    directory = ROOT / "dist"
    record = json.loads((directory / "release-evidence.json").read_text(encoding="utf-8"))
    expected = {"schema": 1, "version": version, "tag": tag,
                "commit": _git("rev-parse", "HEAD"),
                "tag_object": _git("rev-parse", f"refs/tags/{tag}"), "accepted": True}
    if any(record.get(key) != value for key, value in expected.items()) \
            or re.fullmatch(re.escape(version) + r"-final-[1-9]\d*", str(record.get("attempt", ""))) is None:
        raise ReleaseError("published evidence is not finalized stable acceptance")
    expected_names = {f"agents_live-{version}-py3-none-any.whl",
                      f"agents_live-{version}.tar.gz", *BOOTSTRAP_ASSETS}
    if set(record.get("artifacts", {})) != expected_names:
        raise ReleaseError("published evidence has an unexpected artifact set")
    for name, digest in record["artifacts"].items():
        if _sha256(directory / name) != digest:
            raise ReleaseError("downloaded release bytes differ from finalized acceptance")
    if {path.name for path in directory.glob("*.whl")} != {f"agents_live-{version}-py3-none-any.whl"} \
            or {path.name for path in directory.glob("*.tar.gz")} != {f"agents_live-{version}.tar.gz"}:
        raise ReleaseError("publication directory contains unrelated package artifacts")


def _check_publish_state(version: str) -> bool:
    """Validate a prepared release and return whether it still needs pushing."""
    _stable_tag_version(f"v{version}")
    if ACTIVE_ATTEMPT is not None:
        _check_finalization(version)
        _run(["git", "fetch", "--quiet", "origin", "main"])
        head = _git("rev-parse", "HEAD")
        origin = _git("rev-parse", "origin/main")
        if origin not in {head, ACTIVE_ATTEMPT["source_commit"]}:
            raise ReleaseError("main changed since final preparation; accept a new RC")
        remote = _git("ls-remote", "--tags", "origin", f"refs/tags/v{version}")
        tag_object = _git("rev-parse", f"refs/tags/v{version}")
        if remote and remote.split()[0] != tag_object:
            raise ReleaseError("remote stable tag conflicts with finalized identity")
        return head != origin or not remote
    if _git("status", "--porcelain"):
        raise ReleaseError("working tree must be clean")
    branch = _git("branch", "--show-current")
    candidate_branch = _candidate_branch(version)
    if branch not in {"main", candidate_branch}:
        raise ReleaseError(
            f"prepared release must run from main or {candidate_branch}")
    _run(["git", "fetch", "--quiet", "origin", "main", "--tags"])
    head = _git("rev-parse", "HEAD")
    origin = _git("rev-parse", "origin/main")
    needs_push = head != origin
    if needs_push:
        if branch != candidate_branch:
            raise ReleaseError(
                f"an unpublished release must remain on {candidate_branch}")
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


def _candidate_wheel(version: str) -> Path:
    preserved = _artifact_store_dir(version) / \
        f"agents_live-{version}-py3-none-any.whl"
    if preserved.is_file():
        return preserved
    wheel = ROOT / "dist" / f"agents_live-{version}-py3-none-any.whl"
    if not wheel.is_file():
        raise ReleaseError(
            f"prepared wheel is missing: {wheel.resolve()}; "
            "rerun with --prepare --resume"
        )
    return wheel


def _artifact_store_dir(version: str) -> Path:
    if ACTIVE_ATTEMPT is not None:
        return _attempt_path() / "artifacts"
    value = _git(
        "rev-parse", "--git-path",
        f"agents-live-release/artifacts-{version}")
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _preserve_release_artifacts(version: str, wheel: Path) -> Path:
    sdist = ROOT / "dist" / f"agents_live-{version}.tar.gz"
    sources = [wheel, sdist, *(ROOT / "dist" / name for name in BOOTSTRAP_ASSETS)]
    for source in sources:
        if not source.is_file():
            raise ReleaseError(f"prepared artifact is missing: {source.resolve()}")
    hashes = {source.name: _sha256(source) for source in sources}
    destination = _artifact_store_dir(version)
    if ACTIVE_ATTEMPT is not None and destination.exists():
        if any(not (destination / name).is_file()
               or _sha256(destination / name) != digest
               for name, digest in hashes.items()):
            raise ReleaseError("attempt artifacts are immutable; allocate a new attempt")
        return destination / wheel.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"staging-{version}-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "artifacts"
        staging.mkdir()
        for source in sources:
            shutil.copy2(source, staging / source.name)
            if _sha256(staging / source.name) != hashes[source.name]:
                raise ReleaseError(f"artifact changed while preserving: {source.name}")
        previous = None
        if destination.exists():
            previous = Path(tempfile.mkdtemp(
                prefix=f"retained-{version}-", dir=destination.parent)) / "artifacts"
            os.replace(destination, previous)
        try:
            os.replace(staging, destination)
        except BaseException:
            if previous is not None:
                os.replace(previous, destination)
            raise
    return destination / wheel.name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _evidence_identity() -> dict[str, str]:
    return {
        "platform": sys.platform,
        "python_version": sys.version.split()[0],
        "workflow_sha256": _sha256(ROOT / ".github" / "workflows" / "test.yml"),
    }


def _acceptance_path(version: str) -> Path:
    return _release_state_path("acceptance", version)


def _preparation_path(version: str) -> Path:
    return _release_state_path("preparation", version)


def _checkpoint_path(version: str) -> Path:
    return _release_state_path("checkpoint", version)


def _artifact_manifest_path(version: str) -> Path:
    if ACTIVE_ATTEMPT is not None:
        return _attempt_path() / f"SHA256SUMS-{version}"
    value = _git(
        "rev-parse", "--git-path",
        f"agents-live-release/SHA256SUMS-{version}")
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _release_state_path(kind: str, version: str) -> Path:
    if ACTIVE_ATTEMPT is not None:
        if ACTIVE_ATTEMPT["version"] != version:
            raise ReleaseError("receipt version does not match the selected attempt")
        return _attempt_path() / f"{kind}.json"
    value = _git(
        "rev-parse", "--git-path",
        f"agents-live-release/{kind}-{version}.json")
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _candidate_branch(version: str) -> str:
    if ACTIVE_ATTEMPT is not None:
        return ACTIVE_ATTEMPT["branch"]
    return f"release/v{version}-candidate"


def _attempt_path() -> Path:
    if ACTIVE_ATTEMPT is None:
        raise ReleaseError("no release attempt selected")
    return _cycle_directory(ACTIVE_ATTEMPT["target"]) / ACTIVE_ATTEMPT["id"]


def _cycle_directory(target: str) -> Path:
    _stable_tag_version(f"v{target}")
    common = Path(_git("rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = ROOT / common
    return common.resolve() / "agents-live-release" / f"cycle-{target}"


def _write_once(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise ReleaseError(f"immutable release evidence already exists: {path.name}")
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(encoded.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _receipt_tag_object(version: str) -> str | None:
    return None if ACTIVE_ATTEMPT is not None else _git(
        "rev-parse", f"refs/tags/v{version}")


def _check_installed_attempt(version: str, wheel: Path) -> None:
    installed = _install_root() / "versions" / version / "generation.json"
    try:
        record = json.loads(installed.read_text(encoding="utf-8"))
        digest = record["provenance"]["sha256"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReleaseError("installed attempt has no verifiable artifact provenance") from exc
    if digest != _sha256(wheel):
        raise ReleaseError(
            "same-version installation contains different bytes; restore another "
            "version and remove the inactive failed version through versions remove "
            "before bootstrapping this attempt; never overwrite a sealed installation")


def _check_finalization(version: str) -> dict:
    if ACTIVE_ATTEMPT is None or ACTIVE_ATTEMPT["kind"] != "final":
        raise ReleaseError("stable finalization requires a final attempt")
    _stable_tag_version(f"v{version}")
    _check_attempt_checkout()
    _check_preparation(version)
    _check_candidate_acceptance(version)
    try:
        record = json.loads((_attempt_path() / "finalization.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError("final stable attempt has not been finalized") from exc
    expected = {
        "attempt": ACTIVE_ATTEMPT["id"], "version": version,
        "commit": _git("rev-parse", "HEAD"), "tag": f"v{version}",
        "tag_object": _git("rev-parse", f"refs/tags/v{version}"),
        "preparation_sha256": _sha256(_preparation_path(version)),
        "acceptance_sha256": _sha256(_acceptance_path(version)), "approved": True,
    }
    if any(record.get(key) != value for key, value in expected.items()) \
            or _git("cat-file", "-t", f"v{version}") != "tag" \
            or _git("rev-parse", f"v{version}^{{commit}}") != expected["commit"]:
        raise ReleaseError("finalization or stable tag identity changed")
    return record


def finalize_attempt() -> None:
    if ACTIVE_ATTEMPT is None or ACTIVE_ATTEMPT["kind"] != "final":
        raise ReleaseError("only an independently accepted final build can be finalized")
    version = ACTIVE_ATTEMPT["version"]
    _check_attempt_checkout()
    _check_preparation(version)
    _check_candidate_acceptance(version)
    destination = _attempt_path() / "finalization.json"
    if destination.exists():
        _check_finalization(version)
        print("Finalization already matches the accepted bytes")
        return
    tag = f"v{version}"
    if _git("ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
        raise ReleaseError("stable tag is already remote; do not replace it")
    head = _git("rev-parse", "HEAD")
    local = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"], cwd=ROOT)
    if local.returncode == 0:
        if _git("cat-file", "-t", tag) != "tag" or _git("rev-parse", f"{tag}^{{commit}}") != head:
            raise ReleaseError("stable tag conflicts with retained evidence; explicitly migrate the legacy tag")
    else:
        _run(["git", "tag", "-a", tag, "-m", f"agents-live {tag}; accepted {ACTIVE_ATTEMPT['id']}"])
    _write_once(destination, {
        "schema": 1, "attempt": ACTIVE_ATTEMPT["id"], "version": version,
        "commit": head, "tag": tag, "tag_object": _git("rev-parse", f"refs/tags/{tag}"),
        "preparation_sha256": _sha256(_preparation_path(version)),
        "acceptance_sha256": _sha256(_acceptance_path(version)), "approved": True,
        "finalized_at": datetime.now(timezone.utc).isoformat(),
    })
    print(f"Finalized {tag} locally; no publication performed")


def reject_attempt(reason: str) -> None:
    if ACTIVE_ATTEMPT is None or not reason.strip():
        raise ReleaseError("rejection requires an attempt and reason")
    directory = _attempt_path()
    if (directory / "finalization.json").exists():
        raise ReleaseError("finalized attempts require inspection; rejection cannot retire their stable tag")
    version = ACTIVE_ATTEMPT["version"]
    if _installed_version() == version:
        raise ReleaseError(
            "restore a retained version with versions activate, verify doctor and "
            "watcher state, then reject this inactive attempt")
    if not _installed_all_json("doctor").get("ok"):
        raise ReleaseError("restored installation must be healthy before rejection")
    checkpoint = _checkpoint_path(version)
    if (directory / "baseline.json").exists():
        checkpoint = directory / "baseline.json"
    elif _acceptance_path(version).exists():
        raise ReleaseError("accepted attempt has no retained restoration baseline; inspect missing evidence")
    if checkpoint.exists():
        baseline = json.loads(checkpoint.read_text(encoding="utf-8"))
        if _status_contract(_installed_all_json("status")) != tuple(
                tuple(row) for row in baseline["contract"]):
            raise ReleaseError("restoration changed the recorded agent state")
        representative = _installed_json(Path(baseline["repo"]), "status")
        if _started_watchers(representative) != tuple(tuple(row) for row in baseline["watchers"]):
            raise ReleaseError("restoration changed the recorded watcher state")
    _write_once(directory / "rejected.json", {
        "schema": 1, "attempt": ACTIVE_ATTEMPT["id"], "reason": reason.strip(),
        "restored_version": _installed_version(),
        "rejected_at": datetime.now(timezone.utc).isoformat(),
    })
    print("Rejected attempt retained. Remove only its inactive installed version "
          "through versions remove before a different same-version final attempt.")


def migrate_legacy_tag(tag: str) -> None:
    version = _stable_tag_version(tag)
    bake = _cycle_configuration()
    legacy = next((item for item in bake["candidate_cycle"].get("history", {}).values()
                   if item.get("kind") == "legacy-candidate"
                   and item.get("status") == "rejected"
                   and item.get("artifact_version") == version), None)
    if legacy is None:
        raise ReleaseError("manifest has no rejected legacy candidate for this tag")
    if _git("ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
        raise ReleaseError("remote immutable tags cannot be migrated")
    directory = _cycle_directory(version)
    receipt_path = directory.parent / f"preparation-{version}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt["commit"] != legacy.get("commit") \
            or receipt["wheel_sha256"] != legacy.get("wheel_sha256") \
            or receipt["sdist_sha256"] != legacy.get("sdist_sha256"):
        raise ReleaseError("legacy preparation does not match recorded rejected evidence")
    for filename, digest in [(receipt[key], receipt[f"{key}_sha256"]) for key in ("wheel", "sdist")] \
            + [(item["path"], item["sha256"]) for item in receipt["installers"]]:
        if _sha256(Path(filename)) != digest:
            raise ReleaseError("legacy artifact hash changed")
    original = receipt["tag_object"]
    if _git("cat-file", "-t", original) != "tag" \
            or _git("rev-parse", f"{original}^{{commit}}") != receipt["commit"]:
        raise ReleaseError("legacy annotated tag identity changed")
    archive = f"refs/tags/archive/legacy-{tag}-{original[:12]}"
    migration = directory / "legacy-tag-migration.json"
    record = {"schema": 1, "tag": tag, "tag_object": original,
              "archive_ref": archive, "preparation_sha256": _sha256(receipt_path)}
    _write_once(migration, record)
    existing = _git("for-each-ref", "--format=%(objectname)", archive)
    if existing and existing != original:
        raise ReleaseError("legacy archive ref conflicts")
    if not existing:
        _run(["git", "update-ref", archive, original, "0" * 40])
    current = _git("for-each-ref", "--format=%(objectname)", f"refs/tags/{tag}")
    if current:
        if current != original:
            raise ReleaseError("stable tag no longer points to the legacy candidate")
        _run(["git", "update-ref", "-d", f"refs/tags/{tag}", original])
    print(f"Preserved original annotated tag object at {archive}; artifacts and receipts unchanged")


def _attempt_identity() -> dict:
    if ACTIVE_ATTEMPT is None:
        return {}
    return {"attempt": ACTIVE_ATTEMPT["id"],
            "source_commit": ACTIVE_ATTEMPT["source_commit"]}


def _check_final_source(accepted_source: str, final_source: str) -> None:
    manifest = ".github/release-channels.toml"
    if _git("merge-base", accepted_source, final_source) != accepted_source:
        raise ReleaseError("accepted RC source has not reached main")
    changed = set(_git("diff", "--name-only", accepted_source, final_source).splitlines())
    if changed - {manifest}:
        raise ReleaseError("final source differs from accepted RC code; accept a new RC")
    original = tomllib.loads(_git("show", f"{accepted_source}:{manifest}"))
    promoted = tomllib.loads(_git("show", f"{final_source}:{manifest}"))
    original["bake"].pop("promotion", None)
    approval = promoted["bake"].pop("promotion", {})
    if original != promoted:
        raise ReleaseError("release policy changed since accepted RC; accept a new RC")
    if approval.get("decision") != "approved" or approval.get("commit") != accepted_source:
        raise ReleaseError("final preparation requires promotion approval for the accepted RC source")
    try:
        datetime.strptime(approval["decided_on"], "%Y-%m-%d")
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseError("promotion approval requires a valid decision date") from exc


def _check_accepted_rc(record: dict) -> None:
    if record["kind"] != "final":
        return
    accepted = _load_attempt(record["accepted_rc"])
    if accepted["kind"] != "rc" or accepted["target"] != record["target"]:
        raise ReleaseError("final attempt refers to an incompatible RC")
    _retained_preparation(accepted, accepted=True)
    path = _cycle_directory(record["target"]) / accepted["id"] / "acceptance.json"
    if _sha256(path) != record["rc_acceptance_sha256"]:
        raise ReleaseError("accepted RC decision changed since final allocation")
    _check_final_source(accepted["source_commit"], record["source_commit"])


def _cycle_configuration() -> dict:
    with (ROOT / ".github" / "release-channels.toml").open("rb") as stream:
        bake = tomllib.load(stream)["bake"]
    if bake.get("candidate_cycle", {}).get("model") != "numbered-rc":
        raise ReleaseError("attempt commands require a numbered RC cycle")
    _stable_tag_version(f"v{bake['version']}")
    return bake


def cycle_status() -> list[dict]:
    target = _cycle_configuration()["version"]
    directory = _cycle_directory(target)
    rows = []
    for path in sorted(directory.glob("*/attempt.json")):
        identifier = path.parent.name
        row = {"attempt": identifier, "state": "reserved"}
        try:
            if (path.parent / "rejected.json").exists():
                rejection = json.loads((path.parent / "rejected.json").read_text(encoding="utf-8"))
                if rejection.get("schema") != 1 or rejection.get("attempt") != identifier \
                        or not rejection.get("reason") or not rejection.get("restored_version"):
                    raise ReleaseError("rejection record is malformed")
                row["state"] = "rejected"
            else:
                record = _load_attempt(identifier)
                row.update(version=record["version"], source_commit=record["source_commit"])
                if (path.parent / "preparation.json").exists():
                    preparation = _retained_preparation(record)
                    row.update(state="prepared", commit=preparation["commit"],
                               wheel_sha256=preparation["wheel_sha256"])
                if (path.parent / "acceptance.json").exists():
                    _retained_preparation(record, accepted=True)
                    _check_accepted_rc(record)
                    row["state"] = "accepted"
                if (path.parent / "finalization.json").exists():
                    final = json.loads((path.parent / "finalization.json").read_text(encoding="utf-8"))
                    if row["state"] != "accepted" or record["kind"] != "final" \
                            or final.get("schema") != 1 or final.get("approved") is not True \
                            or final.get("attempt") != identifier or final.get("version") != record["version"] \
                            or final.get("tag") != f"v{record['version']}" \
                            or final.get("commit") != row["commit"] \
                            or final.get("preparation_sha256") != _sha256(path.parent / "preparation.json") \
                            or final.get("acceptance_sha256") != _sha256(path.parent / "acceptance.json") \
                            or _git("rev-parse", f"refs/tags/v{record['version']}") != final.get("tag_object") \
                            or _git("cat-file", "-t", final["tag_object"]) != "tag" \
                            or _git("rev-parse", f"{final['tag_object']}^{{commit}}") != row["commit"]:
                        raise ReleaseError("finalization evidence is stale")
                    row["state"] = "finalized"
        except (OSError, ValueError, KeyError, TypeError, ReleaseError, subprocess.CalledProcessError):
            row["state"] = "invalid-evidence"
        rows.append(row)
    return rows


def _load_attempt(identifier: str) -> dict:
    match = re.fullmatch(
        r"((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))(rc[1-9]\d*|-final-[1-9]\d*)",
        identifier)
    if match is None:
        raise ReleaseError("invalid release attempt identity")
    target = match.group(1)
    directory = _cycle_directory(target) / identifier
    try:
        record = json.loads((directory / "attempt.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError("attempt reservation is missing or incomplete") from exc
    kind = "rc" if match.group(2).startswith("rc") else "final"
    if not isinstance(record, dict):
        raise ReleaseError("attempt reservation must be an object")
    expected = {"schema": 1, "id": identifier, "target": target,
                "kind": kind, "version": identifier if kind == "rc" else target,
                "branch": f"release/v{identifier}-candidate"}
    if any(record.get(key) != value for key, value in expected.items()):
        raise ReleaseError("attempt reservation identity is inconsistent")
    if re.fullmatch(r"[0-9a-f]{40}", str(record.get("source_commit", ""))) is None:
        raise ReleaseError("attempt source commit is invalid")
    if (directory / "rejected.json").exists():
        raise ReleaseError("attempt is rejected; retain its evidence and use a new identity")
    return record


def _retained_preparation(record: dict, *, accepted: bool = False) -> dict:
    directory = _cycle_directory(record["target"]) / record["id"]
    try:
        commit = json.loads((directory / "commit.json").read_text(encoding="utf-8"))["commit"]
        preparation = json.loads((directory / "preparation.json").read_text(encoding="utf-8"))
        checkout = preparation["checkout"]
        if not isinstance(checkout, str) or not Path(checkout).is_absolute():
            raise ReleaseError("retained preparation checkout is invalid")
        gate_commands = [[checkout if argument == str(ROOT) else argument for argument in command]
                         for command in _gate_commands()]
        expected = {"schema": PREPARATION_SCHEMA, "prepared": True,
                    "attempt": record["id"], "version": record["version"],
                    "source_commit": record["source_commit"], "commit": commit,
                    "base_commit": record["source_commit"], "tag_object": None,
                    "tag": f"v{record['version']}", "gates": gate_commands,
                    **_evidence_identity()}
        if any(preparation.get(key) != value for key, value in expected.items()):
            raise ReleaseError("retained preparation identity is stale")
        artifacts = [(preparation[key], preparation[f"{key}_sha256"])
                     for key in ("wheel", "sdist")]
        artifacts.extend((asset["path"], asset["sha256"])
                         for asset in preparation["installers"])
        names = {f"agents_live-{record['version']}-py3-none-any.whl",
                 f"agents_live-{record['version']}.tar.gz", *BOOTSTRAP_ASSETS}
        if len(artifacts) != 4 or {Path(filename).name for filename, _digest in artifacts} != names:
            raise ReleaseError("retained preparation has an incomplete artifact set")
        for filename, digest in artifacts:
            path = Path(filename)
            if path.parent.resolve() != (directory / "artifacts").resolve() \
                    or _sha256(path) != digest:
                raise ReleaseError("retained artifact location or hash changed")
        if accepted:
            receipt = json.loads((directory / "acceptance.json").read_text(encoding="utf-8"))
            identity = {key: preparation[key] for key in (
                "attempt", "source_commit", "version", "commit", "tag_object",
                "wheel", "wheel_sha256", "platform", "python_version", "workflow_sha256")}
            identity.update(schema=ACCEPTANCE_SCHEMA, accepted=True, operational=True,
                            preparation_sha256=_sha256(directory / "preparation.json"))
            if any(receipt.get(key) != value for key, value in identity.items()) \
                    or not receipt.get("started_watchers") \
                    or not receipt.get("operational_agent") or not receipt.get("cost_agent"):
                raise ReleaseError("retained operational acceptance is missing or stale")
        return preparation
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReleaseError("retained attempt evidence is incomplete") from exc


def prepare_cycle(*, rc: str | None = None, from_rc: str | None = None) -> None:
    _require_tools()
    bake = _cycle_configuration()
    target = bake["version"]
    branch = bake["branch"] if rc else "main"
    if _git("status", "--porcelain") or _git("branch", "--show-current") != branch:
        raise ReleaseError(f"prepare from a clean synchronized {branch} checkout")
    _run(["git", "fetch", "--quiet", "origin", branch])
    source = _git("rev-parse", "HEAD")
    if source != _git("rev-parse", f"origin/{branch}"):
        raise ReleaseError("preparation source must match origin")
    accepted_record = None
    if rc:
        if re.fullmatch(re.escape(target) + r"rc[1-9]\d*", rc) is None:
            raise ReleaseError("RC must belong to the configured stable target")
        cycle = bake["candidate_cycle"]
        if cycle.get("next") != rc or rc in cycle.get("history", {}):
            raise ReleaseError("RC must be the configured next unused identity")
    else:
        if not from_rc:
            raise ReleaseError("final preparation requires --from-rc")
        accepted_record = _load_attempt(from_rc)
        if accepted_record["kind"] != "rc" or accepted_record["target"] != target:
            raise ReleaseError("final preparation requires an RC of the current target")
        _retained_preparation(accepted_record, accepted=True)
        accepted_source = accepted_record["source_commit"]
        _check_final_source(accepted_source, source)
    directory = _cycle_directory(target)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "allocation.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise ReleaseError("another allocation is active; inspect the retained lock") from exc
    try:
        remote = _git("ls-remote", "--heads", "--tags", "origin")
        remote += "\n" + _git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags")
        consumed = set(bake["candidate_cycle"].get("history", {}))
        consumed.update(path.name for path in directory.iterdir())
        common = Path(_git("rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = ROOT / common
        old_candidates = common / "agents-live-local-deploy" / "candidates"
        if old_candidates.is_dir():
            consumed.update(path.name for path in old_candidates.iterdir())
        consumed.update(re.findall(re.escape(target) + r"(?:rc[1-9]\d*|-final-[1-9]\d*)", remote))
        if rc:
            if rc in consumed or any(
                    item.startswith(target + "rc") and int(item.split("rc")[-1]) >= int(rc.split("rc")[-1])
                    for item in consumed if re.fullmatch(re.escape(target) + r"rc[1-9]\d*", item)):
                raise ReleaseError("RC identity is consumed; advance the manifest to a new RC")
            identifier = rc
        else:
            numbers = [int(item.rsplit("-", 1)[1]) for item in consumed
                       if re.fullmatch(re.escape(target) + r"-final-[1-9]\d*", item)]
            identifier = f"{target}-final-{max(numbers, default=0) + 1}"
        attempt_dir = directory / identifier
        attempt_dir.mkdir()
        record = {"schema": 1, "id": identifier, "target": target,
                  "version": rc or target, "kind": "rc" if rc else "final",
                  "branch": f"release/v{identifier}-candidate", "source_commit": source,
                  "accepted_rc": from_rc,
                  "reserved_at": datetime.now(timezone.utc).isoformat()}
        if accepted_record:
            record["rc_acceptance_sha256"] = _sha256(
                directory / accepted_record["id"] / "acceptance.json")
        _write_once(attempt_dir / "attempt.json", record)
    finally:
        lock.rmdir()
    checkout = directory / "worktrees" / identifier
    checkout.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "-b", record["branch"], str(checkout), source])
    print(f"Reserved {identifier}; retained worktree: {checkout}", flush=True)
    _run(["uv", "run", "--directory", str(checkout), "--script",
          str(checkout / "tools" / "release.py"), "--prepare-attempt", identifier, "--yes"])


def _check_attempt_checkout() -> None:
    if ACTIVE_ATTEMPT is None:
        raise ReleaseError("select an attempt explicitly")
    record = ACTIVE_ATTEMPT
    _check_accepted_rc(record)
    commit = json.loads((_attempt_path() / "commit.json").read_text(encoding="utf-8"))["commit"]
    if _git("status", "--porcelain") or _git("rev-parse", "HEAD") != commit \
            or _git("branch", "--show-current") != record["branch"]:
        raise ReleaseError("attempt requires its clean, exact retained checkout")
    if _git("rev-parse", "HEAD^") != record["source_commit"]:
        raise ReleaseError("attempt source ancestry changed")
    allowed = {path.relative_to(ROOT).as_posix() for path in RELEASE_FILES}
    if set(_git("diff", "--name-only", "HEAD^", "HEAD").splitlines()) - allowed:
        raise ReleaseError("attempt commit contains non-release changes")
    if _current_version() != record["version"]:
        raise ReleaseError("attempt package version changed")


def prepare_attempt() -> None:
    if ACTIVE_ATTEMPT is None:
        raise ReleaseError("select an attempt explicitly")
    record = ACTIVE_ATTEMPT
    directory = _attempt_path()
    version = record["version"]
    commit_path = directory / "commit.json"
    if not commit_path.exists():
        _check_accepted_rc(record)
        if _git("status", "--porcelain") or _git("rev-parse", "HEAD") != record["source_commit"] \
                or _git("branch", "--show-current") != record["branch"]:
            raise ReleaseError("uncommitted attempt changed; retain it for inspection")
        current = _current_version()
        if record["kind"] == "final":
            _update_versions(current, version)
        else:
            _run(["uv", "version", version, "--no-sync"])
            _replace_once(VERSION_FILES[0], f'__version__ = "{current}"', f'__version__ = "{version}"')
            _replace_once(VERSION_FILES[1], f"{current}\n", f"{version}\n")
        validated = {path: path.read_bytes() for path in RELEASE_FILES}
        _run(["git", "add", *[str(path.relative_to(ROOT)) for path in RELEASE_FILES]])
        _run(["git", "commit", "-m", f"chore(build): prepare {record['id']}"])
        for path, content in validated.items():
            if _git("rev-parse", f"HEAD:{path.relative_to(ROOT).as_posix()}") != _blob_id(path, content):
                raise ReleaseError("release metadata changed during commit")
        _write_once(commit_path, {"commit": _git("rev-parse", "HEAD")})
    _check_attempt_checkout()
    if _preparation_path(version).exists():
        _check_preparation(version)
        print(f"Reused exact preparation for {record['id']}")
        return
    build_record = directory / "build.json"
    for command in _gate_commands():
        if "--build-artifacts" in command:
            if build_record.exists():
                retained = json.loads(build_record.read_text(encoding="utf-8"))
                if retained != _release_identity(version, _candidate_wheel(version)):
                    raise ReleaseError("retained build identity changed; never rebuild this attempt")
                (ROOT / "dist").mkdir(exist_ok=True)
                for path in _artifact_store_dir(version).iterdir():
                    shutil.copy2(path, ROOT / "dist" / path.name)
                continue
            if _artifact_store_dir(version).exists() or any((ROOT / "dist").glob("*.whl")):
                raise ReleaseError("unreceipted build bytes exist; retain them and allocate a new attempt")
            _run(command)
            wheel = _preserve_release_artifacts(version, ROOT / "dist" / f"agents_live-{version}-py3-none-any.whl")
            _write_once(build_record, _release_identity(version, wheel))
        else:
            _run(command)
        _check_attempt_checkout()
    wheel = _candidate_wheel(version)
    if json.loads(build_record.read_text(encoding="utf-8")) != _release_identity(version, wheel):
        raise ReleaseError("attempt artifacts changed during readiness")
    _write_preparation(version, wheel)
    print(f"Prepared {record['id']} without a tag. Bootstrap exact wheel: {wheel}")
    print(f"Run --accept-candidate --attempt {record['id']} with the required live-agent arguments.")


def _release_identity(version: str, wheel: Path) -> dict[str, object]:
    sdist = wheel.parent / f"agents_live-{version}.tar.gz"
    if not sdist.is_file():
        raise ReleaseError(
            f"prepared source distribution is missing: {sdist.resolve()}")
    installers = []
    for name in BOOTSTRAP_ASSETS:
        path = wheel.parent / name
        if not path.is_file():
            raise ReleaseError(
                f"prepared bootstrap asset is missing: {path.resolve()}")
        installers.append({
            "path": path.resolve().as_posix(),
            "sha256": _sha256(path),
        })
    return {
        "version": version,
        "tag": f"v{version}",
        "tag_object": _receipt_tag_object(version),
        **_attempt_identity(),
        "commit": _git("rev-parse", "HEAD"),
        "base_commit": _git("rev-parse", "HEAD^"),
        "wheel": wheel.resolve().as_posix(),
        "wheel_sha256": _sha256(wheel),
        "sdist": sdist.resolve().as_posix(),
        "sdist_sha256": _sha256(sdist),
        "installers": installers,
    }


def _write_preparation(version: str, wheel: Path) -> Path:
    destination = _preparation_path(version)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": PREPARATION_SCHEMA,
        "prepared": True,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        **_release_identity(version, wheel),
        **_evidence_identity(),
        "gates": _gate_commands(),
    }
    if ACTIVE_ATTEMPT is not None:
        payload["checkout"] = str(ROOT.resolve())
        _write_once(destination, payload)
        return destination
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def _check_preparation(version: str) -> dict:
    receipt_path = _preparation_path(version)
    recovery_guidance = (
        f"To resume preparation: uv run --script tools/release.py --prepare --resume --yes. "
        "To abandon this candidate, retain its branch, tag, and Git-local artifacts "
        "for inspection and prepare a different version from clean main."
    )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(
            f"prepared release {version} has no gate receipt. {recovery_guidance}"
        ) from exc
    wheel = _candidate_wheel(version)
    expected = {
        "schema": PREPARATION_SCHEMA,
        "prepared": True,
        **_release_identity(version, wheel),
        **_evidence_identity(),
        "gates": _gate_commands(),
    }
    if ACTIVE_ATTEMPT is not None:
        expected["checkout"] = str(ROOT.resolve())
    mismatched = [
        key for key, value in expected.items() if receipt.get(key) != value
    ]
    if mismatched:
        raise ReleaseError(
            f"preparation receipt is stale for: {', '.join(mismatched)}. {recovery_guidance}"
        )
    return receipt


def _write_artifact_manifest(version: str, preparation: dict) -> Path:
    destination = _artifact_manifest_path(version)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for path_key, hash_key in (
        ("wheel", "wheel_sha256"),
        ("sdist", "sdist_sha256"),
    ):
        path = Path(str(preparation[path_key]))
        lines.append(f"{preparation[hash_key]}  {path.name}")
    for installer in preparation["installers"]:
        path = Path(str(installer["path"]))
        lines.append(f"{installer['sha256']}  {path.name}")
    destination.write_text("\n".join(lines) + "\n", encoding="ascii")
    return destination


def _install_root() -> Path:
    explicit_root = os.environ.get("AGENTS_LIVE_INSTALL_ROOT", "").strip()
    if explicit_root:
        return Path(explicit_root).expanduser()
    if os.name == "nt":
        return Path(os.environ.get(
            "LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "agents-live"
    return Path(os.environ.get(
        "XDG_DATA_HOME", Path.home() / ".local" / "share")) / "agents-live"


def _installed_cli() -> str:
    install_root = _install_root()
    filename = "agents-live.exe" if os.name == "nt" else "agents-live"
    command_dir = "Scripts" if os.name == "nt" else "bin"
    self_managed = install_root / "current" / command_dir / filename
    if self_managed.is_file():
        return str(self_managed.resolve())
    raise ReleaseError(
        "an active self-managed agents-live command is required for "
        "candidate acceptance")


def _installed_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("AGENTS_LIVE_REPO", None)
    return subprocess.run(
        [_installed_cli(), *argv], cwd=ROOT, env=environment,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False)


def _installed_version() -> str:
    completed = _installed_run(["--version"])
    match = re.fullmatch(
        r"agents-live ([0-9][0-9A-Za-z.+-]*)"
        r"(?: \(channel: [a-z]+(?:, commit: [0-9a-f]+)?\))?\s*",
        completed.stdout,
    )
    if completed.returncode != 0 or match is None:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ReleaseError(
            f"could not read installed candidate version: {detail}")
    return match.group(1)


def _installed_json(repo: Path, command: str) -> dict:
    completed = _installed_run(
        ["--json", "--repo", str(repo), command])
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ReleaseError(
            f"installed candidate {command} returned invalid JSON: {detail}"
        ) from exc
    if completed.returncode != 0 or not isinstance(payload, dict):
        detail = payload.get("error", payload) if isinstance(payload, dict) else payload
        raise ReleaseError(
            f"installed candidate {command} failed: {detail}")
    return payload


def _installed_all_json(command: str) -> dict:
    completed = _installed_run(["--json", command, "--all-repos"])
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ReleaseError(
            f"installed candidate {command} --all-repos returned invalid "
            f"JSON: {detail}") from exc
    if completed.returncode != 0 or not isinstance(payload, dict):
        detail = payload.get("error", payload) if isinstance(payload, dict) else payload
        raise ReleaseError(
            f"installed candidate {command} --all-repos failed: {detail}")
    return payload


def _status_contract(payload: dict) -> tuple[tuple[object, ...], ...]:
    rows = _status_rows(payload)
    required = ("repository", "identifier", "state")
    for row in rows:
        if (
            not all(isinstance(row.get(field), str) and row.get(field)
                    for field in required)
            or not isinstance(row.get("loadable"), bool)
        ):
            raise ReleaseError(
                f"installed candidate status has a malformed agent row: {row}")
    return tuple(sorted(
        (
            str(row.get("repository", "")),
            str(row.get("identifier", "")),
            str(row.get("state", "")),
            bool(row.get("loadable")),
        )
        for row in rows
    ))


def _status_rows(payload: dict) -> list[dict]:
    rows = payload.get("agents")
    if isinstance(rows, list):
        if not all(isinstance(row, dict) for row in rows):
            raise ReleaseError(
                "installed candidate status has a non-object agent row")
        return rows
    repositories = payload.get("repos")
    if not isinstance(repositories, list):
        raise ReleaseError("installed candidate status has no agent results")
    found: list[dict] = []
    for item in repositories:
        if not isinstance(item, dict) or not item.get("ok"):
            raise ReleaseError(
                f"installed candidate status has an unhealthy repository: {item}")
        result = item.get("result")
        agents = result.get("agents") if isinstance(result, dict) else None
        if not isinstance(agents, list):
            raise ReleaseError(
                f"installed candidate status has no agents for {item.get('name')}")
        if not all(isinstance(row, dict) for row in agents):
            raise ReleaseError(
                "installed candidate status has a non-object agent row for "
                f"{item.get('name')}")
        found.extend(agents)
    return found


def _started_watchers(payload: dict) -> tuple[tuple[str, str], ...]:
    rows = payload.get("agents", [])
    watched = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("state") == "started"
        and isinstance(row.get("execution"), dict)
        and row["execution"].get("watch")
        and row.get("identifier")
    ]
    if any(row.get("ownership_available") is not True for row in watched):
        raise ReleaseError(
            "started watcher ownership is unavailable; candidate acceptance "
            "cannot establish the local watcher baseline")
    return tuple(sorted(
        (str(row.get("repository", "")), str(row.get("identifier")))
        for row in watched
        if row.get("is_owner") is True
    ))


def _write_candidate_acceptance(
    version: str,
    repo: Path,
    wheel: Path,
    *,
    operation_id: str | None,
    watchers: tuple[tuple[str, str], ...],
    operational_agent: str,
    cost_agent: str,
) -> Path:
    destination = _acceptance_path(version)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": ACCEPTANCE_SCHEMA,
        "accepted": True,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "tag": f"v{version}",
        "tag_object": _receipt_tag_object(version),
        **_attempt_identity(),
        "commit": _git("rev-parse", "HEAD"),
        "wheel": wheel.resolve().as_posix(),
        "wheel_sha256": _sha256(wheel),
        "repo": str(repo),
        **_evidence_identity(),
        "operation_id": operation_id,
        "operational": True,
        "operational_agent": operational_agent,
        "cost_agent": cost_agent,
        "started_watchers": [
            {"repo": root, "identifier": watcher}
            for root, watcher in watchers
        ],
    }
    if ACTIVE_ATTEMPT is not None:
        payload["preparation_sha256"] = _sha256(_preparation_path(version))
        _write_once(destination, payload)
        return destination
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def _check_candidate_acceptance(version: str) -> dict:
    receipt_path = _acceptance_path(version)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(
            "prepared candidate has not passed installed-tool acceptance; "
            "run --accept-candidate --repo <live-repository> "
            "--agent <safe-agent-identifier> "
            "--cost-agent <safe-provider-agent-identifier> --yes") from exc
    wheel = _candidate_wheel(version)
    expected = {
        "schema": ACCEPTANCE_SCHEMA,
        "accepted": True,
        "version": version,
        "tag": f"v{version}",
        "tag_object": _receipt_tag_object(version),
        **_attempt_identity(),
        "commit": _git("rev-parse", "HEAD"),
        "wheel": wheel.resolve().as_posix(),
        "wheel_sha256": _sha256(wheel),
        **_evidence_identity(),
    }
    if ACTIVE_ATTEMPT is not None:
        expected["preparation_sha256"] = _sha256(_preparation_path(version))
        if not isinstance(receipt.get("started_watchers"), list) or not receipt["started_watchers"]:
            raise ReleaseError("attempt acceptance requires a recorded started watcher")
    mismatched = [key for key, value in expected.items()
                  if receipt.get(key) != value]
    if receipt.get("operational") is not True \
            or not isinstance(receipt.get("operational_agent"), str) \
            or not receipt["operational_agent"] \
            or not isinstance(receipt.get("cost_agent"), str) \
            or not receipt["cost_agent"]:
        mismatched.append("operational")
    if mismatched:
        raise ReleaseError(
            "candidate acceptance receipt is stale for: "
            + ", ".join(mismatched)
            + "; rerun --accept-candidate")
    return receipt


def _check_release_diff() -> None:
    changed = set(_git("diff", "--name-only").splitlines())
    staged = set(_git("diff", "--cached", "--name-only").splitlines())
    untracked = set(
        _git("ls-files", "--others", "--exclude-standard").splitlines())
    expected = {path.relative_to(ROOT).as_posix() for path in RELEASE_FILES}
    if changed != expected or staged or untracked:
        raise ReleaseError(
            "version bump changed an unexpected file set: "
            f"expected unstaged {sorted(expected)}, got unstaged "
            f"{sorted(changed)}, staged {sorted(staged)}, and untracked "
            f"{sorted(untracked)}"
        )
    _run(["git", "diff", "--check"])


def _check_release_index() -> None:
    """Require the validated release snapshot, with no later worktree edits."""
    changed = set(_git("diff", "--name-only").splitlines())
    staged = set(_git("diff", "--cached", "--name-only").splitlines())
    untracked = set(
        _git("ls-files", "--others", "--exclude-standard").splitlines())
    expected = {path.relative_to(ROOT).as_posix() for path in RELEASE_FILES}
    if changed or staged != expected or untracked:
        raise ReleaseError(
            "staged release changed before commit: "
            f"expected staged {sorted(expected)}, got unstaged "
            f"{sorted(changed)}, staged {sorted(staged)}, and untracked "
            f"{sorted(untracked)}"
        )
    _run(["git", "diff", "--cached", "--check"])


def _blob_id(path: Path, content: bytes) -> str:
    """Hash content exactly as Git would store it after clean filters."""
    relative = path.relative_to(ROOT).as_posix()
    result = subprocess.run(
        ["git", "hash-object", "--path", relative, "--stdin"],
        cwd=ROOT,
        input=content,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("ascii").strip()


def _check_release_commit(validated: dict[Path, bytes]) -> None:
    """Verify the commit contains exactly the bytes that passed the gates."""
    expected = {path.relative_to(ROOT).as_posix() for path in RELEASE_FILES}
    changed = set(_git("diff", "--name-only", "HEAD^..HEAD").splitlines())
    if changed != expected:
        raise ReleaseError(
            "release commit has an unexpected file set: "
            f"expected {sorted(expected)}, got {sorted(changed)}"
        )
    mismatched = []
    for path, content in validated.items():
        relative = path.relative_to(ROOT).as_posix()
        if _git("rev-parse", f"HEAD:{relative}") != _blob_id(path, content):
            mismatched.append(relative)
    if mismatched:
        raise ReleaseError(
            "release files changed after validation: "
            f"{sorted(mismatched)}"
        )


def _smoketest_command() -> list[str]:
    """The end-to-end gate, pinned to this checkout.

    The framework smoketest exercises the real trigger/run/status loop,
    catching breaks the unit suite cannot (e.g. module argv contract
    drift). ``--repo`` is what makes "this checkout" true: without it the
    smoketest acts on whatever root resolves, which on a host with a
    configured default is some other project entirely.
    """
    return ["uv", "run", "--with-editable", ".", "agents-live",
            "--repo", str(ROOT), "smoketest"]


def _build_release_artifacts() -> None:
    with tempfile.TemporaryDirectory(prefix="agents-live-release-build-") as temp:
        temporary = Path(temp)
        archive = temporary / "source.tar"
        _run([
            "git", "archive", "--format=tar", f"--output={archive}", "HEAD",
        ])
        source = temporary / "source"
        shutil.unpack_archive(archive, source, filter="data")
        for path in (*RELEASE_FILES, *BOOTSTRAP_BUILD_INPUTS):
            relative = path.relative_to(ROOT)
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        _run([
            "uv", "build", "--out-dir", str(ROOT / "dist"), str(source),
        ])
        version = _current_version()
        for name in BOOTSTRAP_ASSETS:
            content = (source / name).read_text(encoding="utf-8")
            marker = BOOTSTRAP_VERSION_MARKERS[name]
            if content.count(marker) != 1:
                raise ReleaseError(
                    f"{name} must contain exactly one release version marker")
            assignment = marker.replace("''", f"'{version}'").replace(
                '""', f'"{version}"')
            (ROOT / "dist" / name).write_text(
                content.replace(marker, assignment), encoding="utf-8")


def _gate_commands() -> list[list[str]]:
    """Everything a release has to pass, in order.

    One list, run by both ``prepare`` and ``publish`` and printed by the
    plan, so the three cannot describe different releases.

    The build comes before the dashboard readiness check because that
    check runs the artifact rather than the source: an editable import
    and a ``--help`` exit are what let two packaged dashboard breaks
    reach releases (#279).
    """
    return [
        ["uv", "run", "--script", "tools/pre-release-audit.py"],
        ["uv", "run", "--with-editable", ".", "--script",
         "tests/test_smoke.py"],
        ["uv", "run", "--with-editable", ".", "--script",
         "tests/test_seams.py"],
        ["uv", "run", "--with-editable", ".", "--script",
         "tests/test_behaviors.py"],
        _smoketest_command(),
        ["uv", "run", "--script", "tools/release.py", "--build-artifacts"],
        ["uv", "run", "--script", "tools/dashboard-readiness.py"],
    ]


def gates() -> None:
    """Run every gate that does not need a live agent CLI.

    The publish workflow calls this instead of restating the list in
    YAML, where a gate once lost a dependency the local run kept and
    failed the release after the tag was pushed (#218).
    """
    smoketest = _smoketest_command()
    for command in _gate_commands():
        if command == smoketest:
            print("+ skipped: the framework smoketest needs a live agent CLI",
                  flush=True)
            continue
        _run(command)


def _print_plan(current: str, target: str, minimum_bump: str) -> None:
    tag = f"v{target}"
    print(f"Release plan: {current} -> {target}")
    print(f"Minimum bump from changelog: {minimum_bump}")
    print("Version files:")
    for path in RELEASE_FILES:
        print(f"  {path.relative_to(ROOT)}")
    print("Commands:")
    commands = (
        f"git switch -c {_candidate_branch(target)}",
        *(shlex.join(command) for command in _gate_commands()),
        f"git commit -m 'chore(build): bump version to {tag}' ...",
        f"git tag -a {tag}",
        "agents-live upgrade --from <target wheel> --candidate  # bootstrap candidate",
        "uv run --script tools/release.py --accept-candidate "
        "--repo <live-repository> --agent <safe-agent-identifier> "
        "--cost-agent <safe-provider-agent-identifier> --yes",
        f"git push --atomic origin HEAD:main {tag}",
        "attach SHA256SUMS manifest from the accepted candidate",
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


def prepare(bump: str, *, resume: bool = False) -> None:
    _require_tools()
    branch = _git("branch", "--show-current")
    if resume and branch.startswith("release/v") and branch.endswith("-candidate"):
        target = branch.removeprefix("release/v").removesuffix("-candidate")
        current = target
        minimum_bump = bump
    else:
        current = _current_version()
        target = _next_version(current, bump)
        minimum_bump = _check_bump(bump)
    if resume:
        print(f"Resume preparation of v{target}; reuse a valid receipt or rerun all release gates.")
    else:
        _print_plan(current, target, minimum_bump)
    _check_prepare_state(target, fetch=True, resume=resume)
    candidate_branch = _candidate_branch(target)
    tag = f"v{target}"

    if not resume:
        original = {path: path.read_bytes() for path in RELEASE_FILES}
        original_head = _git("rev-parse", "HEAD")
        release_head: str | None = None
        committed = False
        try:
            _run(["git", "switch", "-c", candidate_branch])
            _update_versions(current, target)
            _check_release_diff()
            validated = {path: path.read_bytes() for path in RELEASE_FILES}
            for command in _gate_commands():
                _run(command)
            # The gates are long and the checkout is shared, so what was
            # validated above is not necessarily what is about to be staged.
            _check_release_diff()
            release_paths = [str(path.relative_to(ROOT)) for path in RELEASE_FILES]
            _run(["git", "add", *release_paths])
            _check_release_index()
            message = f"chore(build): bump version to v{target}"
            _run(["git", "commit", "-m", message])
            release_head = _git("rev-parse", "HEAD")
            committed = True
            _check_release_commit(validated)
        except BaseException:
            committed = _git("rev-parse", "HEAD") != original_head
            if (
                committed
                and release_head is not None
                and _git("rev-parse", "HEAD") == release_head
                and _git("rev-parse", "HEAD^") == original_head
            ):
                subprocess.run(
                    ["git", "reset", "--soft", original_head],
                    cwd=ROOT,
                    check=False,
                )
                committed = False
            if not committed:
                subprocess.run(
                    ["git", "reset", "--quiet", "HEAD", "--",
                     *[str(path.relative_to(ROOT)) for path in RELEASE_FILES]],
                    cwd=ROOT,
                    check=False,
                )
                for path, content in original.items():
                    path.write_bytes(content)
                if _git("branch", "--show-current") == candidate_branch:
                    subprocess.run(
                        ["git", "switch", "main"], cwd=ROOT, check=False)
                    subprocess.run(
                        ["git", "branch", "-D", candidate_branch],
                        cwd=ROOT, check=False)
                print("Restored release files after the failed preparation.", file=sys.stderr)
            raise
    else:
        validated = {path: path.read_bytes() for path in RELEASE_FILES}
        _check_release_commit(validated)
        try:
            _check_preparation(target)
        except (ReleaseError, OSError, subprocess.CalledProcessError):
            pass
        else:
            print(f"Prepared {tag}; existing preparation receipt is valid: {_preparation_path(target)}")
            return

    release_head = _git("rev-parse", "HEAD")
    try:
        if resume:
            for command in _gate_commands():
                _run(command)
            if _git("rev-parse", "HEAD") != release_head or _git("status", "--porcelain"):
                raise ReleaseError("candidate changed while running preparation gates")
            _check_release_commit(validated)
        tag_exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"],
            cwd=ROOT,
        ).returncode == 0
        if not tag_exists:
            _run(["git", "tag", "-a", tag, "-m", f"agents-live {tag}"])
        else:
            if _git("cat-file", "-t", tag) != "tag":
                raise ReleaseError(f"tag {tag} must be annotated")
            tag_commit = _git("rev-parse", f"{tag}^{{commit}}")
            if tag_commit != release_head:
                raise ReleaseError(
                    f"tag {tag} already exists but points to {tag_commit[:8]}, "
                    f"not candidate commit {release_head[:8]}"
                )
        wheel = _preserve_release_artifacts(
            target, ROOT / "dist" / f"agents_live-{target}-py3-none-any.whl")
        receipt = _write_preparation(target, wheel)
    except BaseException as exc:
        raise ReleaseError(
            f"post-commit preparation failed for {tag} ({exc}). "
            f"Candidate commit {release_head[:8]} on branch {candidate_branch} and any "
            "preserved artifacts have been retained for recovery. "
            f"To resume preparation: uv run --script tools/release.py --prepare --bump {bump} --resume --yes. "
            "To abandon this candidate, retain its branch, tag, and Git-local artifacts "
            "for inspection and prepare a different version from clean main."
        ) from exc
    print(f"Prepared {tag}. Inspect dist/ and the commit, then run:")
    print(f"  preparation receipt: {receipt}")
    print(f"  agents-live upgrade --from {wheel} --candidate")
    print("  uv run --script tools/release.py --accept-candidate "
            "--repo <live-repository> --agent <safe-agent-identifier> "
            "--cost-agent <safe-provider-agent-identifier> --yes")
    print("  # after an operational failure with restored state, add --resume")
    print("  uv run --script tools/release.py --publish --yes")


def _run_operational_acceptance(
    repo: Path, agent_id: str, cost_agent: str, *, preflight: bool = False,
) -> None:
    command = [
        "uv", "run", "--script", "tools/candidate-operational.py",
        "--cli", _installed_cli(),
        "--repo", str(repo),
        "--agent", agent_id,
        "--cost-agent", cost_agent,
    ]
    if preflight:
        command.append("--preflight")
    _run(command)


def candidate_preflight(repo: Path, agent_id: str, cost_agent: str) -> None:
    """Reject live-host acceptance blockers before release preparation."""
    _require_tools()
    root = repo.expanduser().resolve()
    if not root.is_dir():
        raise ReleaseError(f"candidate test repository does not exist: {root}")
    _run_operational_acceptance(root, agent_id, cost_agent, preflight=True)
    print(
        "Candidate preflight passed; the selected agents, browser, dashboard "
        "state, ownership, watcher residency, and repository health are ready."
    )


def _write_acceptance_checkpoint(
    version: str,
    repo: Path,
    wheel: Path,
    *,
    operation_id: str | None,
    contract: tuple[tuple[object, ...], ...],
    watchers: tuple[tuple[str, str], ...],
    operational_agent: str,
    cost_agent: str,
) -> Path:
    destination = _checkpoint_path(version)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": CHECKPOINT_SCHEMA,
        "phase": "upgrade-complete",
        "written_at": datetime.now(timezone.utc).isoformat(),
        **_release_identity(version, wheel),
        "repo": str(repo),
        "platform": sys.platform,
        "operation_id": operation_id,
        "contract": [list(row) for row in contract],
        "watchers": [list(row) for row in watchers],
        "operational_agent": operational_agent,
        "cost_agent": cost_agent,
    }
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def _check_acceptance_checkpoint(
    version: str, repo: Path, wheel: Path,
    operational_agent: str, cost_agent: str,
) -> dict:
    path = _checkpoint_path(version)
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(
            "candidate acceptance has no resumable upgrade checkpoint") from exc
    expected = {
        "schema": CHECKPOINT_SCHEMA,
        "phase": "upgrade-complete",
        **_release_identity(version, wheel),
        "repo": str(repo),
        "platform": sys.platform,
        "operational_agent": operational_agent,
        "cost_agent": cost_agent,
    }
    mismatched = [
        key for key, value in expected.items() if checkpoint.get(key) != value
    ]
    if mismatched:
        raise ReleaseError(
            "candidate checkpoint is stale for: " + ", ".join(mismatched))
    contract = checkpoint.get("contract")
    watchers = checkpoint.get("watchers")
    if not isinstance(contract, list) or not contract \
            or not isinstance(watchers, list) or not watchers:
        raise ReleaseError("candidate checkpoint has no restorable baseline")
    for row in contract:
        if (
            not isinstance(row, list)
            or len(row) != 4
            or not all(isinstance(value, str) and value for value in row[:3])
            or not isinstance(row[3], bool)
        ):
            raise ReleaseError(
                f"candidate checkpoint has a malformed status row: {row}")
    for row in watchers:
        if (
            not isinstance(row, list)
            or len(row) != 2
            or not all(isinstance(value, str) and value for value in row)
        ):
            raise ReleaseError(
                f"candidate checkpoint has a malformed watcher row: {row}")
    if checkpoint.get("operation_id") is not None:
        raise ReleaseError(
            "candidate checkpoint has a retired deferred operation ID")
    return checkpoint


def _finish_operational_acceptance(
    version: str,
    root: Path,
    wheel: Path,
    *,
    operation_id: str | None,
    before_contract: tuple[tuple[object, ...], ...],
    watchers: tuple[tuple[str, str], ...],
    operational_agent: str,
    cost_agent: str,
) -> Path:
    _run_operational_acceptance(root, operational_agent, cost_agent)
    final_status = _installed_all_json("status")
    final_doctor = _installed_all_json("doctor")
    if _status_contract(final_status) != before_contract:
        raise ReleaseError(
            "candidate operational pass did not restore repository state")
    if not final_doctor.get("ok"):
        raise ReleaseError(
            "candidate operational pass left repository health degraded")
    final_representative = _installed_json(root, "status")
    if _started_watchers(final_representative) != watchers:
        raise ReleaseError(
            "candidate operational pass changed the representative watchers")
    if ACTIVE_ATTEMPT is not None:
        _check_attempt_checkout()
        _check_preparation(version)
        _check_installed_attempt(version, wheel)
        if _installed_version() != version:
            raise ReleaseError("installed version changed during operational acceptance")
    return _write_candidate_acceptance(
        version, root, wheel, operation_id=operation_id, watchers=watchers,
        operational_agent=operational_agent, cost_agent=cost_agent)


def accept_candidate(
    repo: Path, agent_id: str, cost_agent: str, *, resume: bool = False,
) -> None:
    """Exercise the installed tagged candidate before any public push."""
    _require_tools()
    version = _current_version()
    if ACTIVE_ATTEMPT is not None:
        _check_attempt_checkout()
        if _acceptance_path(version).exists():
            _check_preparation(version)
            _check_candidate_acceptance(version)
            print("Exact attempt already accepted; reject it explicitly to invalidate the decision")
            return
    else:
        _acceptance_path(version).unlink(missing_ok=True)
        _check_publish_state(version)
    _check_preparation(version)
    wheel = _candidate_wheel(version)
    root = repo.expanduser().resolve()
    if not root.is_dir():
        raise ReleaseError(f"candidate test repository does not exist: {root}")
    installed = _installed_version()
    if installed != version:
        raise ReleaseError(
            f"installed tool is {installed}, but prepared candidate is {version}; "
            f"bootstrap it first with `agents-live upgrade --from {wheel} --candidate`")
    if ACTIVE_ATTEMPT is not None:
        _check_installed_attempt(version, wheel)

    _run_operational_acceptance(root, agent_id, cost_agent, preflight=True)

    if resume:
        checkpoint = _check_acceptance_checkpoint(
            version, root, wheel, agent_id, cost_agent)
        before_contract = tuple(
            tuple(row) for row in checkpoint.get("contract", []))
        watchers = tuple(
            (str(row[0]), str(row[1]))
            for row in checkpoint.get("watchers", [])
            if isinstance(row, list) and len(row) == 2)
        current_status = _installed_all_json("status")
        current_doctor = _installed_all_json("doctor")
        current_representative = _installed_json(root, "status")
        if _status_contract(current_status) != before_contract:
            raise ReleaseError(
                "candidate state changed since the resumable checkpoint")
        if not current_doctor.get("ok"):
            raise ReleaseError(
                "candidate repository is unhealthy at resume")
        if _started_watchers(current_representative) != watchers:
            raise ReleaseError(
                "candidate watchers changed since the resumable checkpoint")
        receipt = _finish_operational_acceptance(
            version, root, wheel,
            operation_id=checkpoint.get("operation_id"),
            before_contract=before_contract,
            watchers=watchers,
            operational_agent=agent_id,
            cost_agent=cost_agent,
        )
        _checkpoint_path(version).unlink(missing_ok=True)
        print(f"Accepted installed candidate {version}; receipt: {receipt}")
        return

    _checkpoint_path(version).unlink(missing_ok=True)

    representative = _installed_json(root, "status")
    before_status = _installed_all_json("status")
    before_doctor = _installed_all_json("doctor")
    if not before_doctor.get("ok"):
        raise ReleaseError("candidate test repository is unhealthy before upgrade")
    watchers = _started_watchers(representative)
    if not watchers:
        raise ReleaseError(
            "candidate acceptance requires at least one started watcher in "
            f"{root}")
    before_contract = _status_contract(before_status)

    if ACTIVE_ATTEMPT is not None:
        _write_once(_attempt_path() / "baseline.json", {
            "schema": 1, **_attempt_identity(), "repo": str(root),
            "contract": before_contract, "watchers": watchers,
        })

    completed = _installed_run(
        ["--repo", str(root), "upgrade", "--from", str(wheel), "--candidate"])
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    if completed.returncode != 0:
        raise ReleaseError(
            f"candidate local-wheel upgrade exited {completed.returncode}")

    if _installed_version() != version:
        raise ReleaseError("installed version changed after same-wheel acceptance")
    after_representative = _installed_json(root, "status")
    after_status = _installed_all_json("status")
    after_doctor = _installed_all_json("doctor")
    if _status_contract(after_status) != before_contract:
        raise ReleaseError(
            "candidate upgrade changed repository started/loadable state")
    if not after_doctor.get("ok"):
        raise ReleaseError("candidate test repository is unhealthy after upgrade")
    if _started_watchers(after_representative) != watchers:
        raise ReleaseError("candidate upgrade did not restore the started watchers")

    _write_acceptance_checkpoint(
        version, root, wheel, operation_id=None,
        contract=before_contract, watchers=watchers,
        operational_agent=agent_id, cost_agent=cost_agent)
    receipt = _finish_operational_acceptance(
        version, root, wheel, operation_id=None,
        before_contract=before_contract, watchers=watchers,
        operational_agent=agent_id, cost_agent=cost_agent)
    _checkpoint_path(version).unlink(missing_ok=True)
    print(f"Accepted installed candidate {version}; receipt: {receipt}")


def publish() -> None:
    _require_tools()
    version = _current_version()
    needs_push = _check_publish_state(version)
    tag = f"v{version}"
    existing = subprocess.run(
        ["gh", "release", "view", tag, "--json", "url,isDraft"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    resume_draft = False
    if existing.returncode == 0:
        try:
            existing_release = json.loads(existing.stdout)
        except json.JSONDecodeError as exc:
            raise ReleaseError(
                f"could not read existing GitHub release {tag}") from exc
        if not existing_release.get("isDraft"):
            print(f"GitHub release {tag} already exists: {existing.stdout.strip()}")
            print(f"  Rerun the notes with: --notes {tag} --yes")
            return
        resume_draft = True
    preparation = _check_preparation(version)
    _check_candidate_acceptance(version)
    if ACTIVE_ATTEMPT is not None:
        preparation = {**preparation, "tag_object": _check_finalization(version)["tag_object"]}
    notes = _release_notes(version)
    manifest = _write_artifact_manifest(version, preparation)
    accepted_artifacts = (
        Path(str(preparation["wheel"])),
        Path(str(preparation["sdist"])),
        *(Path(str(asset["path"])) for asset in preparation["installers"]),
    )
    evidence_assets = ()
    if ACTIVE_ATTEMPT is not None:
        evidence = _attempt_path() / "release-evidence.json"
        _write_once(evidence, {
            "schema": 1, "version": version, "tag": tag, "accepted": True,
            "attempt": ACTIVE_ATTEMPT["id"], "commit": preparation["commit"],
            "tag_object": preparation["tag_object"],
            "artifacts": {path.name: _sha256(path) for path in accepted_artifacts},
            "preparation_sha256": _sha256(_preparation_path(version)),
            "acceptance_sha256": _sha256(_acceptance_path(version)),
            "finalization_sha256": _sha256(_attempt_path() / "finalization.json"),
        })
        evidence_assets = (evidence,)
    if needs_push:
        _run([
            "git", "push", "--atomic", "origin",
            f"{preparation['commit']}:refs/heads/main",
            f"{preparation['tag_object']}:refs/tags/{tag}",
        ])
    _write_release_notes(
        tag, notes, create=True, assets=(manifest, *accepted_artifacts, *evidence_assets),
        resume_draft=resume_draft)
    print(f"Published GitHub release {tag}; the PyPI workflow is now running.")
    print(f"Record the local release decision: agents-live versions classify {version} released")


def main(argv: list[str] | None = None) -> int:
    global ACTIVE_ATTEMPT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-rc", metavar="VERSION")
    parser.add_argument("--prepare-final", action="store_true")
    parser.add_argument("--from-rc", metavar="ATTEMPT")
    parser.add_argument("--prepare-attempt", metavar="ATTEMPT")
    parser.add_argument("--attempt", metavar="ATTEMPT")
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--reject-attempt", metavar="ATTEMPT")
    parser.add_argument("--reason")
    parser.add_argument("--migrate-legacy-tag", metavar="TAG")
    parser.add_argument("--verify-publication-assets", metavar="TAG")
    parser.add_argument("--cycle-status", action="store_true")
    parser.add_argument("--verify-publication", metavar="TAG",
                        help="Verify stable-only workflow publication prerequisites")
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
        "--accept-candidate",
        action="store_true",
        help="Reinstall and verify the prepared candidate before publication",
    )
    parser.add_argument(
        "--candidate-preflight",
        action="store_true",
        help="Check live candidate prerequisites before release preparation",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        help="Live repository used by --accept-candidate",
    )
    parser.add_argument(
        "--agent",
        help="Safe live agent exercised by CLI and dashboard acceptance",
    )
    parser.add_argument(
        "--cost-agent",
        help="Safe provider-backed agent required to report list cost",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume candidate preparation after a post-commit failure, or acceptance after a verified upgrade checkpoint",
    )
    parser.add_argument(
        "--gates",
        action="store_true",
        help="Run the release gates that do not need a live agent CLI",
    )
    parser.add_argument(
        "--build-artifacts",
        action="store_true",
        help=argparse.SUPPRESS,
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
    selected = sum((args.dry_run, args.prepare, args.publish,
                    args.accept_candidate, args.candidate_preflight,
                    args.gates, args.build_artifacts, args.notes is not None,
                    args.verify_publication is not None, args.prepare_rc is not None,
                    args.prepare_final, args.prepare_attempt is not None,
                    args.finalize, args.reject_attempt is not None,
                    args.migrate_legacy_tag is not None,
                    args.verify_publication_assets is not None, args.cycle_status))
    if selected != 1:
        parser.error("choose exactly one release operation; see --help")
    cycle_write = (args.prepare_rc or args.prepare_final or args.prepare_attempt
                   or args.finalize or args.reject_attempt or args.migrate_legacy_tag)
    if cycle_write and not args.yes:
        parser.error("release attempt changes require --yes")
    if args.from_rc and not args.prepare_final or args.prepare_final and not args.from_rc:
        parser.error("--prepare-final requires --from-rc; --from-rc applies only to final preparation")
    if args.attempt and not (args.accept_candidate or args.finalize or args.publish):
        parser.error("--attempt applies only to acceptance, finalization, or publication")
    if args.finalize and not args.attempt:
        parser.error("--finalize requires --attempt")
    if bool(args.reason) != bool(args.reject_attempt):
        parser.error("--reject-attempt requires --reason")
    if (args.prepare or args.accept_candidate or args.publish) and not args.yes:
        parser.error("--prepare, --accept-candidate, and --publish require --yes")
    if (args.accept_candidate or args.candidate_preflight) and (
            args.repo is None or not args.agent or not args.cost_agent):
        parser.error(
            "candidate preflight and acceptance require --repo, --agent, "
            "and --cost-agent")
    if (args.repo is not None or args.agent is not None
            or args.cost_agent is not None) \
            and not (args.accept_candidate or args.candidate_preflight):
        parser.error(
            "--repo, --agent, and --cost-agent apply only to "
            "candidate preflight or acceptance")
    if args.resume and not (args.accept_candidate or args.prepare):
        parser.error("--resume applies only to --accept-candidate and --prepare")
    if (args.publish or args.accept_candidate or args.candidate_preflight
            or args.gates or args.notes) \
            and args.bump != "patch":
        parser.error(
            "--bump applies only to --dry-run and --prepare")
    mutation_lock = None
    try:
        identifier = args.attempt or args.prepare_attempt or args.reject_attempt
        ACTIVE_ATTEMPT = _load_attempt(identifier) if identifier else None
        if ACTIVE_ATTEMPT is not None:
            lock = _cycle_directory(ACTIVE_ATTEMPT["target"]) / "mutation.lock"
            try:
                lock.mkdir()
            except FileExistsError as exc:
                raise ReleaseError("another cycle operation is active; inspect the retained mutation lock") from exc
            mutation_lock = lock
            ACTIVE_ATTEMPT = _load_attempt(identifier)
        if args.cycle_status:
            with contextlib.redirect_stdout(sys.stderr):
                status = cycle_status()
            print(json.dumps(status, indent=2))
            return 0
        if args.verify_publication:
            verify_publication(args.verify_publication)
            return 0
        if args.verify_publication_assets:
            verify_publication_assets(args.verify_publication_assets)
            return 0
        if args.prepare_rc or args.prepare_final:
            prepare_cycle(rc=args.prepare_rc, from_rc=args.from_rc)
            return 0
        if args.prepare_attempt:
            prepare_attempt()
            return 0
        if args.finalize:
            finalize_attempt()
            return 0
        if args.reject_attempt:
            reject_attempt(args.reason)
            return 0
        if args.migrate_legacy_tag:
            migrate_legacy_tag(args.migrate_legacy_tag)
            return 0
        if args.dry_run or args.prepare or args.accept_candidate or args.publish:
            manifest = ROOT / ".github" / "release-channels.toml"
            if manifest.is_file():
                with manifest.open("rb") as stream:
                    cycle = tomllib.load(stream).get("bake", {}).get("candidate_cycle", {})
                if cycle.get("model") == "numbered-rc" and ACTIVE_ATTEMPT is None:
                    raise ReleaseError(
                        "the numbered RC workflow requires --prepare-rc or --prepare-final, "
                        "and --attempt for acceptance or publication. The legacy "
                        "stable-numbered candidate workflow remains blocked under #511.")
        if args.dry_run:
            preview(args.bump)
        elif args.prepare:
            prepare(args.bump, resume=args.resume)
        elif args.candidate_preflight:
            assert args.repo is not None
            assert args.agent is not None
            assert args.cost_agent is not None
            candidate_preflight(args.repo, args.agent, args.cost_agent)
        elif args.accept_candidate:
            assert args.repo is not None
            assert args.agent is not None
            assert args.cost_agent is not None
            accept_candidate(
                args.repo, args.agent, args.cost_agent, resume=args.resume)
        elif args.gates:
            gates()
        elif args.build_artifacts:
            _build_release_artifacts()
        elif args.notes:
            notes(args.notes, apply=args.yes)
        else:
            publish()
    except KeyboardInterrupt:
        print("release interrupted", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError, ReleaseError, subprocess.CalledProcessError) as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 1
    finally:
        if mutation_lock is not None:
            mutation_lock.rmdir()
        ACTIVE_ATTEMPT = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())