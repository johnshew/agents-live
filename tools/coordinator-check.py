#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic coordinator check: report what changed since the last run.

Usage:
  uv run --script tools/coordinator-check.py [--state PATH] [--stall-minutes N]

One shot, read-only apart from its own state file. Lookups run in parallel
under one deadline (``RUN_SECONDS``); a lookup still running at the deadline
is reported as UNKNOWN rather than waited for. It checks:

- ``origin/main``: commits new since the last run (after a capped fetch).
- The primary checkout: on main, clean, and not behind ``origin/main``.
- Every other worktree, except retained release worktrees under the common Git
  directory: an open PR or uncommitted edits with no activity for
  ``--stall-minutes`` is STALLED; a clean worktree whose head is on
  ``origin/main`` and that has been quiet for two hours is removable; commits
  that are on no open PR are flagged.
- Open pull requests: failing checks, conflicts, behind main, or ready for a
  merge decision.
- The installed runtime: its version, and error records logged since the last
  run in each registered repository.

It reports changes only. Each finding has a band (ok, concern, red, or
unknown for a failed lookup). A finding prints when it first leaves ok, when
its band changes, and when it clears; an unchanged red or unknown finding is
repeated every ``REMIND_AFTER``. New main commits, runtime version changes and
new runtime errors are events and print every time they occur. When nothing
changed it prints nothing.

Exit 0 when nothing printed, 1 when something changed, 2 when any lookup
failed. The state file is written after the output; the main position only
advances when no lookup failed, so a failed run reports the same commits again.

Output never carries an absolute path, so it can be forwarded as-is. It never
fetches into, switches, resets, stashes or cleans a worktree, and never starts,
stops or changes the installed runtime.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / ".reports" / "coordinator-check" / "state.json"
MAIN = "origin/main"
RUN_SECONDS = 60.0
COMMAND_TIMEOUT = 45
FETCH_TIMEOUT = 15
REMIND_AFTER = timedelta(hours=2)
QUIET_BEFORE_REMOVABLE = timedelta(hours=2)
DEFAULT_SINCE = timedelta(minutes=30)
MAX_SINCE = timedelta(hours=24)
BANDS = ("red", "unknown", "concern", "ok")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Finding:
    key: str
    band: str
    text: str


@dataclass(frozen=True)
class Worktree:
    name: str
    branch: str
    dirty: bool
    on_main: bool
    ahead: int
    last_activity: datetime


def run(args: list[str], cwd: Path = ROOT, timeout: float = COMMAND_TIMEOUT) -> str:
    completed = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, check=False, creationflags=NO_WINDOW)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise RuntimeError(f"{args[0]} {args[1]} failed: {detail[-1] if detail else completed.returncode}")
    return completed.stdout


def git(*args: str, cwd: Path = ROOT, timeout: float = COMMAND_TIMEOUT) -> str:
    return run(["git", *args], cwd=cwd, timeout=timeout).strip()


# --- Pure classification -----------------------------------------------------

def classify_worktree(tree: Worktree, pr: dict[str, Any] | None, now: datetime,
                      stall: timedelta) -> Finding:
    key = f"worktree:{tree.name}"
    label = f"worktree {tree.name} ({tree.branch or 'detached'})"
    quiet = now - tree.last_activity
    if pr is not None or tree.dirty:
        if quiet > stall:
            minutes = int(quiet.total_seconds() // 60)
            return Finding(key, "concern", f"STALLED {label}: no edits or commits for {minutes} min")
        return Finding(key, "ok", f"{label} active")
    if tree.on_main and quiet > QUIET_BEFORE_REMOVABLE:
        return Finding(key, "concern", f"HYGIENE {label} is clean and merged; removable")
    if tree.ahead:
        return Finding(key, "concern", f"UNPUBLISHED {label} has {tree.ahead} commit(s) on no open PR")
    return Finding(key, "ok", f"{label} quiet")


def classify_pr(pr: dict[str, Any]) -> Finding:
    key = f"pr:{pr['number']}"
    label = f"PR #{pr['number']} {pr.get('title', '')}".strip()
    checks = pr.get("statusCheckRollup") or []
    conclusions = {str(c.get("conclusion") or c.get("state") or "").upper() for c in checks}
    pending = any(str(c.get("status") or "").upper() not in ("", "COMPLETED") for c in checks)
    if conclusions & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
        return Finding(key, "red", f"{label}: checks failing")
    state = str(pr.get("mergeStateStatus") or "").upper()
    if state == "UNKNOWN":
        return Finding(key, "carry", f"{label}: merge state not yet computed")
    if state == "DIRTY":
        return Finding(key, "concern" if pr.get("isDraft") else "red", f"{label}: merge conflicts")
    if state == "BEHIND":
        return Finding(key, "concern", f"{label}: behind main")
    if pr.get("isDraft") or pending:
        return Finding(key, "ok", f"{label}: in progress")
    if state == "CLEAN":
        return Finding(key, "concern", f"{label}: checks passed; ready for a merge decision")
    return Finding(key, "ok", f"{label}: {state.lower() or 'open'}")


def classify_primary(branch: str, dirty: bool, behind: int) -> Finding:
    if branch != "main":
        return Finding("primary", "concern", f"primary checkout is on {branch or 'a detached head'}, not main")
    if dirty:
        return Finding("primary", "concern", "primary checkout has uncommitted changes")
    if behind:
        return Finding("primary", "concern", f"primary checkout is {behind} commit(s) behind {MAIN}; fast-forward it")
    return Finding("primary", "ok", "primary checkout current")


def compare(previous: dict[str, dict[str, Any]], findings: list[Finding],
            now: datetime) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Lines to print and the next finding state; change-only by band."""
    lines: list[tuple[int, str]] = []
    state: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for finding in findings:
        seen.add(finding.key)
        before = previous.get(finding.key)
        if finding.band == "carry":
            if before is not None:
                state[finding.key] = before
            continue
        if finding.band == "ok":
            if before is not None:
                lines.append((3, f"CLEARED {finding.text}"))
            continue
        rank = BANDS.index(finding.band)
        tag = finding.band.upper()
        reported = now
        if before is None or before.get("band") != finding.band:
            lines.append((rank, f"{tag} {finding.text}"))
        elif finding.band in ("red", "unknown") and now - _time(before.get("reported_at")) >= REMIND_AFTER:
            lines.append((rank, f"{tag} (still) {finding.text}"))
        else:
            reported = _time(before.get("reported_at"))
        state[finding.key] = {"band": finding.band, "text": finding.text,
                              "reported_at": reported.isoformat()}
    for key, before in previous.items():
        if key not in seen:
            lines.append((3, f"CLEARED {before.get('text', key)}"))
    return [text for _rank, text in sorted(lines, key=lambda item: item[0])], state


def exit_code(lines: list[str], events: list[str], failed: bool) -> int:
    if failed:
        return 2
    return 1 if lines or events else 0


def _time(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


# --- Collection --------------------------------------------------------------

def scrubber() -> Callable[[str], str]:
    replacements = [(str(ROOT.parent), "<repos>"), (str(Path.home()), "~")]

    def scrub(text: str) -> str:
        for prefix, label in replacements:
            text = text.replace(prefix, label).replace(prefix.replace("\\", "/"), label)
        return text
    return scrub


def fetch() -> None:
    git("fetch", "origin", "--prune", "--quiet", timeout=FETCH_TIMEOUT)


def worktrees() -> tuple[Path, list[dict[str, str]]]:
    """The primary checkout and the other worktrees, minus retained release ones."""
    common = Path(git("rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (ROOT / common).resolve()
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in git("worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if current:
                entries.append(current)
            current = {}
            continue
        field, _, value = line.partition(" ")
        current[field] = value
    retained = str(common.resolve()).lower()
    primary = Path(entries[0]["worktree"])
    return primary, [e for e in entries[1:]
                     if not str(Path(e["worktree"]).resolve()).lower().startswith(retained)]


def inspect_worktree(entry: dict[str, str]) -> Worktree:
    path = Path(entry["worktree"])
    branch = entry.get("branch", "").removeprefix("refs/heads/")
    status = git("status", "--porcelain", "--untracked-files=all", cwd=path)
    times = [float(git("log", "-1", "--format=%ct", cwd=path) or 0)]
    private = Path(git("rev-parse", "--absolute-git-dir", cwd=path))
    for marker in (private / "index", private / "logs" / "HEAD", private / "HEAD"):
        if marker.exists():
            times.append(marker.stat().st_mtime)
    for line in status.splitlines():
        name = line[3:].split(" -> ")[-1].strip('"')
        candidate = path / name
        if candidate.exists():
            times.append(candidate.stat().st_mtime)
    head = entry.get("HEAD", "")
    on_main = subprocess.run(
        ["git", "merge-base", "--is-ancestor", head, MAIN], cwd=path,
        capture_output=True, check=False, creationflags=NO_WINDOW).returncode == 0
    ahead = int(git("rev-list", "--count", f"{MAIN}..{head}", cwd=path) or 0) if head else 0
    return Worktree(path.name, branch, bool(status), on_main, ahead,
                    datetime.fromtimestamp(max(times), timezone.utc))


def open_prs() -> list[dict[str, Any]]:
    out = run(["gh", "pr", "list", "--state", "open", "--json",
               "number,title,headRefName,isDraft,mergeStateStatus,statusCheckRollup"])
    return json.loads(out or "[]")


def registered_repos() -> list[str]:
    names = []
    for line in run(["agents-live", "repos", "list"]).splitlines():
        name = line.split("\t", 1)[0].removesuffix(" (default)").strip()
        if name:
            names.append(name)
    return names


def runtime_errors(repo: str, since: datetime) -> list[dict[str, Any]]:
    out = run(["agents-live", "--repo", repo, "--json", "logs", "--all", "--errors",
               "--since", since.strftime("%Y-%m-%dT%H:%M:%SZ"), "-n", "200"])
    return json.loads(out).get("records", [])


def collect(previous: dict[str, Any], now: datetime, stall: timedelta,
            scrub: Callable[[str], str]) -> tuple[list[Finding], list[str], dict[str, Any], bool]:
    findings: list[Finding] = []
    events: list[str] = []
    failed = False
    since = (max(_time(previous["last_run"]), now - MAX_SINCE)
             if previous.get("last_run") else now - DEFAULT_SINCE)

    def unknown(key: str, error: BaseException) -> None:
        nonlocal failed
        failed = True
        findings.append(Finding(key, "unknown", scrub(f"{key} lookup failed: {error}")))

    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = pool.submit(fetch)
        prs = pool.submit(open_prs)
        repos = pool.submit(registered_repos)
        version = pool.submit(run, ["agents-live", "--version"])
        deadline = datetime.now(timezone.utc) + timedelta(seconds=RUN_SECONDS)

        def result(future: Any, key: str) -> Any:
            remaining = max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
            try:
                return future.result(timeout=remaining)
            except Exception as error:  # noqa: BLE001 - every failure is reported as UNKNOWN
                unknown(key, error if str(error) else TimeoutError("run deadline"))
                return None

        error_futures = {}
        names = result(repos, "repos")
        for name in names or []:
            error_futures[name] = pool.submit(runtime_errors, name, since)

        fetch_ok = result(fetched, "fetch") is None and not any(f.key == "fetch" for f in findings)
        main = ""
        try:
            main = git("rev-parse", MAIN)
            before = previous.get("main")
            if before and before != main and fetch_ok:
                subjects = git("log", "--format=%h %s", f"{before}..{main}").splitlines()
                shown = "; ".join(subjects[:5]) + ("; ..." if len(subjects) > 5 else "")
                events.append(f"MAIN-NEW {len(subjects)} commit(s) on {MAIN}: {shown}")
        except Exception as error:  # noqa: BLE001
            unknown("main", error)

        try:
            primary, entries = worktrees()
        except Exception as error:  # noqa: BLE001
            primary, entries = None, []
            unknown("worktrees", error)
        if primary is not None:
            try:
                branch = git("branch", "--show-current", cwd=primary)
                dirty = bool(git("status", "--porcelain", "--untracked-files=no", cwd=primary))
                behind = int(git("rev-list", "--count", f"HEAD..{MAIN}", cwd=primary) or 0)
                findings.append(classify_primary(branch, dirty, behind))
            except Exception as error:  # noqa: BLE001
                unknown("primary", error)

        pr_list = result(prs, "prs") or []
        by_branch = {pr.get("headRefName"): pr for pr in pr_list}
        findings.extend(classify_pr(pr) for pr in pr_list)

        tree_futures = {entry["worktree"]: pool.submit(inspect_worktree, entry) for entry in entries}
        for path, future in tree_futures.items():
            tree = result(future, f"worktree:{Path(path).name}")
            if tree is not None:
                findings.append(classify_worktree(tree, by_branch.get(tree.branch), now, stall))

        installed = (result(version, "runtime-version") or "").strip()
        if installed and previous.get("runtime_version") and installed != previous["runtime_version"]:
            events.append(f"RUNTIME installed version is now {installed} (was {previous['runtime_version']})")

        for name, future in error_futures.items():
            records = result(future, f"runtime:{name}")
            if records:
                agents = sorted({str(r.get("agent_name")) for r in records})
                events.append(scrub(
                    f"ERRORS {name}: {len(records)} new error record(s) since "
                    f"{since:%H:%M} UTC from {', '.join(agents[:6])}"))

    extra = {"main": main if fetch_ok and not failed else previous.get("main", main),
             "runtime_version": installed or previous.get("runtime_version", "")}
    return findings, events, extra, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--stall-minutes", type=int, default=20)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    previous: dict[str, Any] = {}
    if args.state.exists():
        try:
            previous = json.loads(args.state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    scrub = scrubber()
    findings, events, extra, failed = collect(
        previous, now, timedelta(minutes=args.stall_minutes), scrub)
    lines, state = compare(previous.get("findings", {}), findings, now)
    output = [scrub(line) for line in events + lines]
    if output:
        print("\n".join(output))
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(
        {"last_run": now.isoformat(), "findings": state, **extra}, indent=2),
        encoding="utf-8")
    return exit_code(lines, events, failed)


if __name__ == "__main__":
    sys.exit(main())
