#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# ///
"""Prepare and publish an agents-live release from a clean main branch."""
from __future__ import annotations

import argparse
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
import time
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
ACCEPTANCE_SCHEMA = 2
PREPARATION_SCHEMA = 2
CHECKPOINT_SCHEMA = 1
QUEUED_UPGRADE_RE = re.compile(
    r"Upgrade queued as (?P<operation>[0-9a-f]+); "
    r"result: (?P<result>.+?); run `agents-live logs admin`"
)


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


def _check_prepare_state(target: str, *, fetch: bool) -> None:
    if _git("status", "--porcelain"):
        raise ReleaseError("working tree must be clean")
    if _git("branch", "--show-current") != "main":
        raise ReleaseError("releases must run from main")
    if fetch:
        _run(["git", "fetch", "--quiet", "origin", "main", "--tags"])
    if _git("rev-parse", "HEAD") != _git("rev-parse", "origin/main"):
        raise ReleaseError("main must match origin/main before release")
    branch = _candidate_branch(target)
    local_branch = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=ROOT,
    )
    if local_branch.returncode == 0:
        raise ReleaseError(
            f"candidate branch {branch} already exists; finish or delete it")
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
            f"prepared wheel is missing: {wheel.relative_to(ROOT)}; "
            "rerun --prepare"
        )
    return wheel


def _artifact_store_dir(version: str) -> Path:
    value = _git(
        "rev-parse", "--git-path",
        f"agents-live-release/artifacts-{version}")
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _preserve_release_artifacts(version: str, wheel: Path) -> Path:
    sdist = ROOT / "dist" / f"agents_live-{version}.tar.gz"
    if not sdist.is_file():
        raise ReleaseError(
            f"prepared source distribution is missing: {sdist.relative_to(ROOT)}")
    destination = _artifact_store_dir(version)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    preserved_wheel = destination / wheel.name
    shutil.copy2(wheel, preserved_wheel)
    shutil.copy2(sdist, destination / sdist.name)
    return preserved_wheel


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
    value = _git(
        "rev-parse", "--git-path",
        f"agents-live-release/SHA256SUMS-{version}")
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _release_state_path(kind: str, version: str) -> Path:
    value = _git(
        "rev-parse", "--git-path",
        f"agents-live-release/{kind}-{version}.json")
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _candidate_branch(version: str) -> str:
    return f"release/v{version}-candidate"


def _release_identity(version: str, wheel: Path) -> dict[str, object]:
    sdist = wheel.parent / f"agents_live-{version}.tar.gz"
    if not sdist.is_file():
        raise ReleaseError(
            f"prepared source distribution is missing: {sdist.relative_to(ROOT)}")
    return {
        "version": version,
        "tag": f"v{version}",
        "tag_object": _git("rev-parse", f"refs/tags/v{version}"),
        "commit": _git("rev-parse", "HEAD"),
        "base_commit": _git("rev-parse", "HEAD^"),
        "wheel": wheel.relative_to(ROOT).as_posix(),
        "wheel_sha256": _sha256(wheel),
        "sdist": sdist.relative_to(ROOT).as_posix(),
        "sdist_sha256": _sha256(sdist),
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
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def _check_preparation(version: str) -> dict:
    receipt_path = _preparation_path(version)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(
            "prepared release has no gate receipt; rerun --prepare") from exc
    wheel = _candidate_wheel(version)
    expected = {
        "schema": PREPARATION_SCHEMA,
        "prepared": True,
        **_release_identity(version, wheel),
        **_evidence_identity(),
        "gates": _gate_commands(),
    }
    mismatched = [
        key for key, value in expected.items() if receipt.get(key) != value
    ]
    if mismatched:
        raise ReleaseError(
            "preparation receipt is stale for: " + ", ".join(mismatched)
            + "; rerun --prepare")
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
    destination.write_text("\n".join(lines) + "\n", encoding="ascii")
    return destination


def _installed_cli() -> str:
    tool_root = Path(_run(["uv", "tool", "dir"], capture=True))
    environment = tool_root / "agents-live"
    filename = "agents-live.exe" if os.name == "nt" else "agents-live"
    candidates = (
        environment / "Scripts" / filename,
        environment / "bin" / filename,
    )
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        raise ReleaseError(
            "the uv-managed agents-live launcher is required for candidate "
            f"acceptance under {environment}")
    return str(executable.resolve())


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


def _wait_for_upgrade_result(
    result_path: Path, *, timeout_s: float = 900.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            last = None
        if isinstance(last, dict) and last.get("status") == "terminal":
            return last
        time.sleep(0.2)
    raise ReleaseError(
        f"candidate upgrade did not reach a terminal result within "
        f"{timeout_s:.0f}s: {last!r}")


def _candidate_events(operation_id: str) -> list[dict]:
    if not re.fullmatch(r"[0-9a-f]+", operation_id):
        raise ReleaseError("candidate upgrade returned an invalid operation ID")
    sql = (
        "select run_id, status, message, attributes from log "
        f"where run_id = '{operation_id}' order by ts"
    )
    completed = _installed_run(
        ["logs", "--all", "--sql", sql, "--format", "jsonl"])
    if completed.returncode != 0:
        raise ReleaseError(
            "could not query candidate upgrade events: "
            f"{completed.stderr.strip() or completed.stdout.strip()}")
    try:
        rows = [json.loads(line) for line in completed.stdout.splitlines() if line]
    except json.JSONDecodeError as exc:
        raise ReleaseError("candidate upgrade events were not valid JSONL") from exc
    return [_decode_candidate_event(row) for row in rows]


def _decode_candidate_event(row: dict) -> dict:
    def scalar(value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value.replace("NULL", "null"))
        except json.JSONDecodeError:
            return value

    event = dict(row)
    values = event.pop("attributes", [])
    if not isinstance(values, list):
        return event
    for value in values:
        try:
            pair = scalar(value)
        except json.JSONDecodeError:
            continue
        if isinstance(pair, list) and len(pair) == 2:
            pair = [scalar(pair[0]), scalar(pair[1])]
        if (
            isinstance(pair, list)
            and len(pair) == 2
            and isinstance(pair[0], str)
            and pair[0] not in event
        ):
            event[pair[0]] = pair[1]
    return event


def _verify_candidate_events(
    events: list[dict], watchers: tuple[tuple[str, str], ...],
) -> None:
    def normalized_root(value: object) -> str:
        path = str(Path(str(value)).resolve())
        return path.casefold() if os.name == "nt" else path

    def watcher_index(phase: str, root: str, watcher: str) -> int:
        wanted_root = normalized_root(root)
        for index, event in enumerate(events):
            if (
                event.get("status") == "ok"
                and event.get("upgrade_phase") == phase
                and event.get("watcher") == watcher
                and normalized_root(event.get("root", "")) == wanted_root
            ):
                return index
        raise ReleaseError(
            f"candidate upgrade has no exact {phase} event for "
            f"{watcher} in {root}")

    plugin_indexes = [
        index for index, event in enumerate(events)
        if event.get("status") == "ok"
        and (
            event.get("operation") == "plugin-converge"
            or event.get("message") in {
                "plugin-converge", "plugins already converged"}
        )
    ]
    terminal_indexes = [
        index for index, event in enumerate(events)
        if event.get("status") == "ok"
        and event.get("message") == "deferred Windows upgrade completed"
    ]
    if not plugin_indexes:
        raise ReleaseError("candidate upgrade has no successful plugin event")
    if not terminal_indexes:
        raise ReleaseError("candidate upgrade has no successful terminal event")
    plugin_index = plugin_indexes[-1]
    terminal_index = terminal_indexes[-1]
    for root, watcher in watchers:
        requested = watcher_index("quiesce-requested", root, watcher)
        quiesced = watcher_index("quiesced", root, watcher)
        restored = watcher_index("restore", root, watcher)
        if not requested < quiesced < plugin_index < restored < terminal_index:
            raise ReleaseError(
                f"candidate upgrade lifecycle is out of order for "
                f"{watcher} in {root}")


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
        "tag_object": _git("rev-parse", f"refs/tags/v{version}"),
        "commit": _git("rev-parse", "HEAD"),
        "wheel": wheel.relative_to(ROOT).as_posix(),
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
        "tag_object": _git("rev-parse", f"refs/tags/v{version}"),
        "commit": _git("rev-parse", "HEAD"),
        "wheel": wheel.relative_to(ROOT).as_posix(),
        "wheel_sha256": _sha256(wheel),
        **_evidence_identity(),
    }
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
        for path in RELEASE_FILES:
            relative = path.relative_to(ROOT)
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        _run([
            "uv", "build", "--out-dir", str(ROOT / "dist"), str(source),
        ])


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
        "agents-live upgrade --from <target wheel>  # bootstrap candidate",
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


def prepare(bump: str) -> None:
    _require_tools()
    current = _current_version()
    target = _next_version(current, bump)
    minimum_bump = _check_bump(bump)
    _print_plan(current, target, minimum_bump)
    _check_prepare_state(target, fetch=True)
    _acceptance_path(target).unlink(missing_ok=True)
    _preparation_path(target).unlink(missing_ok=True)
    _checkpoint_path(target).unlink(missing_ok=True)
    shutil.rmtree(_artifact_store_dir(target), ignore_errors=True)
    original = {path: path.read_bytes() for path in RELEASE_FILES}
    original_head = _git("rev-parse", "HEAD")
    candidate_branch = _candidate_branch(target)
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

    tag = f"v{target}"
    _run(["git", "tag", "-a", tag, "-m", f"agents-live {tag}"])
    wheel = _preserve_release_artifacts(
        target, ROOT / "dist" / f"agents_live-{target}-py3-none-any.whl")
    receipt = _write_preparation(target, wheel)
    print(f"Prepared {tag}. Inspect dist/ and the commit, then run:")
    print(f"  preparation receipt: {receipt}")
    print(f"  agents-live upgrade --from {wheel}")
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
    operation_id = checkpoint.get("operation_id")
    if operation_id is not None and (
        not isinstance(operation_id, str)
        or re.fullmatch(r"[0-9a-f]+", operation_id) is None
    ):
        raise ReleaseError("candidate checkpoint has an invalid operation ID")
    if os.name == "nt" and operation_id is None:
        raise ReleaseError(
            "Windows candidate checkpoint has no deferred operation ID")
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
    return _write_candidate_acceptance(
        version, root, wheel, operation_id=operation_id, watchers=watchers,
        operational_agent=operational_agent, cost_agent=cost_agent)


def accept_candidate(
    repo: Path, agent_id: str, cost_agent: str, *, resume: bool = False,
) -> None:
    """Exercise the installed tagged candidate before any public push."""
    _require_tools()
    version = _current_version()
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
            f"bootstrap it first with `agents-live upgrade --from {wheel}`")

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

    completed = _installed_run(
        ["--repo", str(root), "upgrade", "--from", str(wheel)])
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    if completed.returncode != 0:
        raise ReleaseError(
            f"candidate local-wheel upgrade exited {completed.returncode}")

    operation_id: str | None = None
    match = QUEUED_UPGRADE_RE.search(completed.stdout)
    if match is not None:
        operation_id = match.group("operation")
        result = _wait_for_upgrade_result(Path(match.group("result").strip()))
        if result.get("operation_id") != operation_id:
            raise ReleaseError("candidate upgrade result has the wrong operation ID")
        if result.get("exit_code") != 0:
            raise ReleaseError(
                f"candidate deferred upgrade exited {result.get('exit_code')}")
    elif os.name == "nt":
        raise ReleaseError(
            "Windows candidate upgrade did not queue a durable helper result")

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

    if operation_id is not None:
        _verify_candidate_events(_candidate_events(operation_id), watchers)
    _write_acceptance_checkpoint(
        version, root, wheel, operation_id=operation_id,
        contract=before_contract, watchers=watchers,
        operational_agent=agent_id, cost_agent=cost_agent)
    receipt = _finish_operational_acceptance(
        version, root, wheel, operation_id=operation_id,
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
    notes = _release_notes(version)
    manifest = _write_artifact_manifest(version, preparation)
    accepted_artifacts = (
        Path(str(preparation["wheel"])),
        Path(str(preparation["sdist"])),
    )
    if needs_push:
        _run([
            "git", "push", "--atomic", "origin",
            f"{preparation['commit']}:refs/heads/main",
            f"{preparation['tag_object']}:refs/tags/{tag}",
        ])
    _write_release_notes(
        tag, notes, create=True, assets=(manifest, *accepted_artifacts),
        resume_draft=resume_draft)
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
        help="Resume candidate acceptance after a verified upgrade checkpoint",
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
                    args.gates, args.build_artifacts, args.notes is not None))
    if selected != 1:
        parser.error(
            "choose exactly one of --dry-run, --prepare, "
            "--candidate-preflight, --accept-candidate, --publish, --gates, "
            "--build-artifacts, or --notes")
    if (args.prepare or args.accept_candidate or args.publish) and not args.yes:
        parser.error("--prepare, --accept-candidate, and --publish require --yes")
    if (args.accept_candidate or args.candidate_preflight) and (
            args.repo is None or not args.agent or not args.cost_agent):
        parser.error(
            "candidate preflight and acceptance require --repo, --agent, "
            "and --cost-agent")
    if (args.repo is not None or args.agent is not None
            or args.cost_agent is not None or args.resume) \
            and not (args.accept_candidate or args.candidate_preflight):
        parser.error(
            "--repo, --agent, --cost-agent, and --resume apply only to "
            "candidate preflight or acceptance")
    if args.resume and not args.accept_candidate:
        parser.error("--resume applies only to --accept-candidate")
    if (args.publish or args.accept_candidate or args.candidate_preflight
            or args.gates or args.notes) \
            and args.bump != "patch":
        parser.error(
            "--bump applies only to --dry-run and --prepare")
    try:
        if args.dry_run:
            preview(args.bump)
        elif args.prepare:
            prepare(args.bump)
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
    except (OSError, ReleaseError, subprocess.CalledProcessError) as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())