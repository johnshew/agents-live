#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Generate the repository's evidence-based release channel report."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".github" / "release-channels.toml"
OUTPUT = ROOT / "docs" / "release-report.md"


class ReportError(RuntimeError):
    pass


def _run(*argv: str) -> str:
    completed = subprocess.run(
        argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ReportError(f"{' '.join(argv)} failed: {detail}")
    return completed.stdout.strip()


def _json(*argv: str) -> Any:
    output = _run(*argv)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise ReportError(f"{' '.join(argv)} returned invalid JSON") from exc


def _sha(ref: str) -> str:
    return _run("git", "rev-parse", ref)


def _count(left: str, right: str) -> tuple[int, int]:
    behind, ahead = _run(
        "git", "rev-list", "--left-right", "--count", f"{left}...{right}"
    ).split()
    return int(behind), int(ahead)


def _link(repository: str, kind: str, number: int) -> str:
    return f"[#{number}](https://github.com/{repository}/{kind}/{number})"


def _checks(pr: dict[str, Any]) -> str:
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return "not reported"
    if any(item.get("status") != "COMPLETED" for item in checks):
        return "pending"
    if any(item.get("conclusion") != "SUCCESS" for item in checks):
        return "failed"
    return "passed"


def _issue_rows(
    repository: str,
    configured: dict[str, list[int]],
) -> tuple[list[str], set[int]]:
    dispositions = (
        ("delivered", "Delivered to bake"),
        ("partial", "Partially delivered"),
        ("deferred", "Deferred"),
    )
    rows: list[str] = []
    assigned: set[int] = set()
    decisions = set(configured.get("promotion_decision", []))
    for key, label in dispositions:
        for number in configured.get(key, []):
            issue = _json(
                "gh", "issue", "view", str(number), "--json",
                "number,title,state,url")
            assigned.add(number)
            decision = "required" if number in decisions and issue["state"] == "OPEN" else "no"
            rows.append(
                f"| {_link(repository, 'issues', number)} | {issue['title']} | "
                f"{label} | {issue['state'].lower()} | {decision} |")
    return rows, assigned


def _render(config: dict[str, Any], generated_at: datetime) -> str:
    repository_data = _json("gh", "repo", "view", "--json", "nameWithOwner,url")
    repository = repository_data["nameWithOwner"]
    release = config["release"]
    bake = config["bake"]
    release_ref = f"origin/{release['branch']}"
    bake_ref = f"origin/{bake['branch']}"
    release_sha = _sha(release_ref)
    bake_sha = _sha(bake_ref)
    _, bake_ahead = _count(release_ref, bake_ref)

    latest = _json(
        "gh", "release", "view", "--json",
        "name,tagName,publishedAt,isDraft,isPrerelease,url")
    tag_sha = _sha(f"{latest['tagName']}^{{commit}}")
    _, release_ahead = _count(latest["tagName"], release_ref)

    pr_fields = (
        "number,title,state,isDraft,baseRefName,headRefName,mergedAt,url,"
        "statusCheckRollup")
    merged = _json(
        "gh", "pr", "list", "--state", "merged", "--base", bake["branch"],
        "--limit", "100", "--json", pr_fields)
    merged_to_release = [
        pr for pr in _json(
            "gh", "pr", "list", "--state", "merged", "--base",
            release["branch"], "--limit", "100", "--json", pr_fields)
        if pr["mergedAt"] and pr["mergedAt"] >= latest["publishedAt"]
    ]
    open_to_bake = _json(
        "gh", "pr", "list", "--state", "open", "--base", bake["branch"],
        "--limit", "100", "--json", pr_fields)
    open_to_release = _json(
        "gh", "pr", "list", "--state", "open", "--base", release["branch"],
        "--limit", "100", "--json", pr_fields)
    promotion = _json(
        "gh", "pr", "list", "--state", "open", "--base", release["branch"],
        "--head", bake["branch"], "--limit", "10", "--json", pr_fields)

    issue_rows, assigned = _issue_rows(repository, bake["issues"])
    decisions = [
        number for number in bake["issues"].get("promotion_decision", [])
        if number in assigned and _json(
            "gh", "issue", "view", str(number), "--json", "state")["state"] == "OPEN"
    ]

    release_date = latest["publishedAt"][:10]
    recent_open = _json(
        "gh", "issue", "list", "--state", "open", "--search",
        f"updated:>={release_date}", "--limit", "100", "--json",
        "number,title,updatedAt,url")
    unassigned = [item for item in recent_open if item["number"] not in assigned]

    deployed_sha = bake["deployed_commit"]
    deployed_in_bake = subprocess.run(
        ["git", "merge-base", "--is-ancestor", deployed_sha, bake_ref],
        cwd=ROOT, check=False).returncode == 0
    deployed_distance = int(_run(
        "git", "rev-list", "--count", f"{deployed_sha}..{bake_ref}"
    )) if deployed_in_bake else -1

    bake_state = "promotion proposed" if promotion else "baking" if bake_ahead else "idle"
    if decisions:
        bake_state += ", promotion decisions open"
    deployed_state = (
        "at tip" if deployed_sha == bake_sha else
        f"behind tip by {deployed_distance} commit(s)" if deployed_in_bake else
        "not in channel history"
    )

    lines = [
        "---",
        "title: Release Channel Report",
        "description: Generated state of work flowing through bake and release channels",
        f"ms.date: {generated_at.date().isoformat()}",
        "ms.topic: reference",
        "---",
        "",
        "<!-- Generated by tools/release-report.py; edit release-channels.toml, not this file. -->",
        "",
        "Point-in-time release report for "
        f"[{repository}]({repository_data['url']}), generated at "
        f"`{generated_at.isoformat().replace('+00:00', 'Z')}`.",
        "",
        "## Executive summary",
        "",
        "| Channel | Branch / artifact | Current state | Next promotion |",
        "|---|---|---|---|",
        f"| Bake | `{bake['branch']}` at `{bake_sha[:8]}` | {bake_state}; "
        f"{bake_ahead} commit(s) ahead of release | Pull request to `{release['branch']}` |",
        f"| Release | `{release['branch']}` at `{release_sha[:8]}`; "
        f"[{latest['tagName']}]({latest['url']}) at `{tag_sha[:8]}` | released; "
        f"{release_ahead} commit(s) on `{release['branch']}` after the latest release | "
        f"{release['promotes_to']} |",
        "",
        f"Promotion readiness: **not ready**. "
        f"{'No bake-to-release pull request is open. ' if not promotion else ''}"
        f"Open promotion decisions: "
        f"{', '.join(_link(repository, 'issues', n) for n in decisions) or 'none'}. "
        f"The deployed bake is {deployed_state}.",
        "",
        "## Channel definitions",
        "",
        "- `bake`: focused PRs integrate on "
        f"`{bake['branch']}` and produce `{bake['version']}.dev*` artifacts. "
        f"This channel promotes to `{bake['promotes_to']}`.",
        "- `release`: reviewed promotion lands on "
        f"`{release['branch']}`; `tools/release.py` creates an ephemeral candidate, "
        "receipt-bound artifacts, an immutable tag, a GitHub Release, and PyPI publication.",
        "",
        "## Bake deployment",
        "",
        "| Version | Commit | Validated | Relation to channel tip |",
        "|---|---|---|---|",
        f"| `{bake['deployed_version']}` | `{deployed_sha[:8]}` | "
        f"`{bake['validated_on']}` | {deployed_state} |",
        "",
        "Deployment identity is reviewed manifest data. Update it only after the exact "
        "artifact has been installed and validated.",
        "",
        "## Pull request flow",
        "",
        f"### Merged into release since {latest['tagName']} ({len(merged_to_release)})",
        "",
        "| PR | Change | Source | Merged | Checks |",
        "|---|---|---|---|---|",
    ]
    for pr in sorted(merged_to_release, key=lambda item: item["mergedAt"] or ""):
        lines.append(
            f"| {_link(repository, 'pull', pr['number'])} | {pr['title']} | "
            f"`{pr['headRefName']}` | `{pr['mergedAt'][:10]}` | {_checks(pr)} |")
    if not merged_to_release:
        lines.append("| - | No pull requests merged since the latest release | - | - | - |")

    lines.extend([
        "",
        f"### Merged into bake ({len(merged)})",
        "",
        "| PR | Change | Source | Merged | Checks |",
        "|---|---|---|---|---|",
    ])
    for pr in sorted(merged, key=lambda item: item["mergedAt"] or ""):
        lines.append(
            f"| {_link(repository, 'pull', pr['number'])} | {pr['title']} | "
            f"`{pr['headRefName']}` | `{pr['mergedAt'][:10]}` | {_checks(pr)} |")
    if not merged:
        lines.append("| - | No pull requests merged into this bake | - | - | - |")

    lines.extend([
        "",
        f"### Open flow ({len(open_to_bake) + len(open_to_release)})",
        "",
    ])
    if not open_to_bake and not open_to_release:
        lines.append("No pull requests currently target bake or promote bake to release.")
    for pr in open_to_bake + open_to_release:
        lines.append(
            f"- {_link(repository, 'pull', pr['number'])} {pr['title']}: "
            f"`{pr['headRefName']}` -> `{pr['baseRefName']}` ({_checks(pr)}).")

    lines.extend([
        "",
        "## Issue disposition",
        "",
        "Open does not mean absent from bake: GitHub closes linked issues only after "
        "the work reaches the default branch.",
        "",
        "| Issue | Work | Bake disposition | GitHub state | Promotion decision |",
        "|---|---|---|---|---|",
        *issue_rows,
        "",
        "### Recent open issues without a channel disposition",
        "",
    ])
    if unassigned:
        for issue in sorted(unassigned, key=lambda item: item["number"], reverse=True):
            lines.append(
                f"- {_link(repository, 'issues', issue['number'])} {issue['title']} "
                f"(updated `{issue['updatedAt'][:10]}`).")
    else:
        lines.append("None since the latest published release.")

    lines.extend([
        "",
        "## Promotion path",
        "",
        f"1. Resolve or explicitly accept open promotion decisions and validate an artifact at `{bake_sha[:8]}`.",
        "2. Run changelog maintenance on bake and commit any release-report updates.",
        f"3. Open one promotion PR from `{bake['branch']}` to `{release['branch']}`.",
        "4. After required Ubuntu and Windows checks pass, merge the promotion.",
        "5. Prepare and accept the receipt-bound official candidate from clean `main`.",
        f"6. Publish `{bake['version']}` to GitHub Releases and PyPI, then regenerate this report.",
        "",
        "## Provenance",
        "",
        f"- Release branch: `{release_sha}`",
        f"- Bake branch: `{bake_sha}`",
        f"- Latest released tag: `{latest['tagName']}` at `{tag_sha}`",
        f"- Manifest: [`.github/release-channels.toml`](../.github/release-channels.toml)",
        f"- Generator: [`tools/release-report.py`](../tools/release-report.py)",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    with CONFIG.open("rb") as stream:
        config = tomllib.load(stream)
    if config.get("schema") != 1:
        raise ReportError("unsupported release channel manifest schema")
    generated_at = datetime.now(timezone.utc)
    if args.check and args.output.exists():
        current = args.output.read_text(encoding="utf-8")
        match = re.search(r"generated at `([^`]+)`", current)
        if match is None:
            print("release report has no generation timestamp", file=sys.stderr)
            return 1
        generated_at = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    report = _render(config, generated_at)
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != report:
            print(f"release report is stale: run {Path(__file__).name}", file=sys.stderr)
            return 1
        return 0
    args.output.write_text(report, encoding="utf-8", newline="\n")
    print(f"Wrote {args.output.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as exc:
        print(f"release report: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc