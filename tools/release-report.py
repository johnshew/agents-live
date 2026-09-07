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
OUTPUT = ROOT / ".reports" / "release-report.md"
REPORT_ONLY_PATHS = {
    ".github/release-channels.toml",
    "tools/release-report.py",
}


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


def _commits(count: int) -> str:
    return f"{count} commit{'s' if count != 1 else ''}"


def _has_runtime_changes(deployed_sha: str, bake_ref: str) -> bool:
    paths = set(_run(
        "git", "diff", "--name-only", f"{deployed_sha}..{bake_ref}"
    ).splitlines())
    return bool(paths - REPORT_ONLY_PATHS)


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


def _promotion_state(bake: dict[str, Any], bake_sha: str) -> tuple[bool, str]:
    promotion = bake.get("promotion")
    if not isinstance(promotion, dict):
        raise ReportError("bake.promotion configuration is missing")
    decision = promotion.get("decision")
    if decision == "continue-bake":
        return False, "The developer has directed this version to remain in bake."
    if decision != "approved":
        raise ReportError(
            "bake.promotion.decision must be 'continue-bake' or 'approved'")
    approved_commit = promotion.get("commit")
    decided_on = promotion.get("decided_on")
    if not isinstance(approved_commit, str) \
            or re.fullmatch(r"[0-9a-f]{40}", approved_commit) is None \
            or not isinstance(decided_on, str) \
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", decided_on) is None:
        raise ReportError(
            "approved bake promotion requires a full commit and decided_on date")
    if approved_commit != bake_sha:
        return False, (
            f"Developer approval names `{approved_commit[:8]}`, but the current "
            f"bake is `{bake_sha[:8]}`. Revalidate it and record a new decision.")
    return True, (
        f"The developer approved bake `{bake_sha[:8]}` for promotion on "
        f"`{decided_on}`.")


def _development_state(
    *, is_released: bool = False, bake_moved: bool, promotion_approved: bool,
    promotion_open: bool,
) -> tuple[str, str]:
    if is_released:
        return (
            "released",
            "The configured release is complete. Direct subsequent development to the next cycle.",
        )
    if bake_moved:
        return (
            "ready for candidate",
            "The approved bake is in `main`; prepare a new official candidate.",
        )
    if promotion_open and promotion_approved:
        return (
            "promotion proposed",
            "The bake-to-`main` pull request is open and must pass its checks.",
        )
    if promotion_approved:
        return (
            "promotion approved",
            "The current bake commit is approved; open the bake-to-`main` pull request.",
        )
    if promotion_open:
        return (
            "baking",
            "A promotion pull request exists without current developer approval; do not merge it.",
        )
    return (
        "baking",
        "Keep fixing, deploying, and validating the configured bake branch.",
    )


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
    for number in configured.get("promotion_decision", []):
        if number in assigned:
            continue
        issue = _json(
            "gh", "issue", "view", str(number), "--json",
            "number,title,state,url")
        assigned.add(number)
        decision = "required" if issue["state"] == "OPEN" else "no"
        rows.append(
            f"| {_link(repository, 'issues', number)} | {issue['title']} | "
            "Awaiting promotion decision | "
            f"{issue['state'].lower()} | {decision} |")
    return rows, assigned


def _render(config: dict[str, Any], generated_at: datetime, *, as_json: bool = False) -> str:
    repository_data = _json("gh", "repo", "view", "--json", "nameWithOwner,url")
    repository = repository_data["nameWithOwner"]
    release = config["release"]
    bake = config["bake"]
    recommendations = bake["recommendations"]
    release_ref = f"origin/{release['branch']}"
    bake_ref = f"origin/{bake['branch']}"
    if subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", bake_ref],
        cwd=ROOT, check=False, capture_output=True,
    ).returncode != 0:
        merged_promotions = _json(
            "gh", "pr", "list", "--state", "merged", "--base", release["branch"],
            "--head", bake["branch"], "--limit", "1", "--json",
            "headRefOid,mergedAt")
        if not merged_promotions:
            raise ReportError(
                f"cannot resolve bake branch {bake['branch']} or a merged promotion")
        bake_ref = merged_promotions[0]["headRefOid"]
    release_sha = _sha(release_ref)
    bake_sha = _sha(bake_ref)
    promotion_approved, promotion_state = _promotion_state(bake, bake_sha)
    _, bake_ahead = _count(release_ref, bake_ref)
    bake_moved = bake_ahead == 0

    latest = _json(
        "gh", "release", "view", "--json",
        "name,tagName,publishedAt,isDraft,isPrerelease,url")
    tag_sha = _sha(f"{latest['tagName']}^{{commit}}")
    _, release_ahead = _count(latest["tagName"], release_ref)
    release_state = (
        f"{latest['tagName']} is public. `main` contains "
        f"{_commits(release_ahead)} of newer work not yet published."
        if release_ahead else
        f"{latest['tagName']} is public and `main` exactly matches it."
    )

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
    is_released = (
        latest.get("tagName", "").removeprefix("v") == str(bake["version"])
        and not latest.get("isDraft")
        and not latest.get("isPrerelease")
    )
    development_state, development_state_detail = _development_state(
        is_released=is_released,
        bake_moved=bake_moved,
        promotion_approved=promotion_approved,
        promotion_open=bool(promotion),
    )

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
    if subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{deployed_sha}^{{commit}}"],
        cwd=ROOT, check=False, capture_output=True,
    ).returncode != 0:
        raise ReportError(
            f"deployed bake commit does not resolve: {deployed_sha}")
    deployed_in_bake = subprocess.run(
        ["git", "merge-base", "--is-ancestor", deployed_sha, bake_ref],
        cwd=ROOT, check=False).returncode == 0
    deployed_distance = int(_run(
        "git", "rev-list", "--count", f"{deployed_sha}..{bake_ref}"
    )) if deployed_in_bake else -1
    runtime_current = deployed_sha == bake_sha or (
        deployed_in_bake and not _has_runtime_changes(deployed_sha, bake_ref)
    )

    deployed_state = (
        "matches the current bake" if deployed_sha == bake_sha else
        "matches the current bake code; only release reporting changed afterward"
        if runtime_current else
        f"is older than the current bake by {_commits(deployed_distance)}" if deployed_in_bake else
        "does not belong to the current bake"
    )
    if is_released:
        can_release = "**No, this release is complete.**"
        release_actions = [
            f"The {bake['version']} release is published. Configure the next bake cycle "
            "in `.github/release-channels.toml` and direct subsequent development to it."
        ]
    else:
        can_release = "**No, not yet.**"
        release_actions = []
        if not bake_moved:
            release_actions.append(promotion_state)
        if not runtime_current:
            release_actions.append(
                "The version installed for testing is not the newest bake. "
                "Fix outstanding bake defects, then install and test the current "
                "bake before considering promotion.")
        if bake_moved:
            release_actions.append(
                "Bake has moved into `main`. Prepare, install, and accept the official candidate.")
        elif not promotion_approved:
            release_actions.append(
                "Do not open or merge the bake-to-main pull request until the "
                "developer records approval for the current bake commit.")
        elif not promotion:
            release_actions.append(
                "After the newest bake is accepted, open a pull request to move "
                "bake into `main`.")
        else:
            release_actions.append(
                "The pull request to move bake into `main` must pass its checks and be merged.")
        if decisions:
            release_actions.append(
                "We still need to decide how to handle "
                f"{', '.join(_link(repository, 'issues', n) for n in decisions)}.")
    recommendation_lines = [
        f"- {_link(repository, 'issues', number)}: "
        f"{recommendations[str(number)]}"
        for number in decisions
        if str(number) in recommendations
    ]
    recommendation_lines.append(f"- Testing: {recommendations['testing']}")
    bake_next = (
        "Configure the next release cycle."
        if is_released else
        "Prepare the official candidate from `main`."
        if bake_moved else
        "Complete the recommendations below and test the newest bake."
        if decisions or not runtime_current else
        "Open a pull request to `main`."
    )
    if is_released:
        next_actions = [
            "Configure the next bake branch and version in `.github/release-channels.toml`.",
            "Direct subsequent development and pull requests to the new bake branch.",
        ]
    else:
        next_actions = []
        if decisions:
            next_actions.append("Resolve the remaining release decisions.")
        if not runtime_current:
            next_actions.append(
                f"Install and test a version built from `{bake_sha[:8]}`.")
        if not bake_moved:
            next_actions.append(
                "Keep fixing and redeploying bake until its newest commit satisfies "
                "every recommendation and is accepted for promotion.")
            next_actions.append(
                "Make sure the changelog describes everything included in bake.")
            if not promotion_approved:
                next_actions.append(
                    "Obtain developer approval for the exact bake commit and record "
                    "it in `.github/release-channels.toml`.")
            if not promotion:
                next_actions.append(
                    f"Only then, open one pull request from `{bake['branch']}` to "
                    f"`{release['branch']}`.")
            next_actions.append(
                "After the Ubuntu and Windows checks pass, merge it into `main`.")
        next_actions.extend([
            "Use the release tool to build the candidate, install it, and complete the final tests.",
            f"Publish `{bake['version']}` to GitHub Releases and PyPI, then regenerate this report.",
        ])
    overall_recommendation = (
        f"The {bake['version']} release is published. Direct subsequent development to the next cycle."
        if is_released else
        f"Bake has moved into `main`. Prepare and accept the official {bake['version']} candidate."
        if bake_moved else recommendations["overall"]
    )
    bake_state = (
        f"{bake['version']} is published."
        if is_released else
        "All bake changes are in `main`."
        if bake_moved else "Work is still being tested. It contains changes not yet in `main`."
    )
    active_bake_guidance = [] if (bake_moved or is_released) else [
        "",
        "## How to improve the current bake",
        "",
        f"- Commit small, simple fixes directly to `{bake['branch']}`.",
        "- For larger work, create a focused branch and open a pull request "
        f"targeting `{bake['branch']}`.",
        f"- If requested work names `{release['branch']}`, confirm whether it "
        "is a bake fix, bake-to-release promotion, or independent post-release "
        "work before changing branches.",
        f"- Use the primary checkout only when it is clean and already on "
        f"`{bake['branch']}`; otherwise, create a dedicated worktree from that "
        "branch and remove it when the task is complete.",
        f"- Verify that work descends from `{bake['branch']}` before committing "
        "or pushing.",
        "- After each change reaches bake, deploy its exact synchronized commit:",
        f"  Run this from a clean checkout of `{bake['branch']}`.",
        "",
        "```bash",
        f"git pull --ff-only origin {bake['branch']}",
        "uv run --script tools/local-deploy.py --repo <live-repository>",
        "```",
    ]

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
        "## Current development state",
        "",
        f"**`{development_state}`**",
        "",
        development_state_detail,
        "",
        "## Can we release this version now?",
        "",
        can_release,
        "",
        " ".join(release_actions) if release_actions else
        "All recorded decisions and bake testing are complete.",
        "",
        "## What we recommend",
        "",
        f"**{overall_recommendation}**",
        "",
        *recommendation_lines,
        "",
        "## Where each channel stands",
        "",
        "| Channel | Branch and version | Where things stand | What happens next |",
        "|---|---|---|---|",
        f"| Bake | `{bake['branch']}` at `{bake_sha[:8]}` | {bake_state} | {bake_next} |",
        f"| Release | `{release['branch']}` at `{release_sha[:8]}`; "
        f"[{latest['tagName']}]({latest['url']}) at `{tag_sha[:8]}` | "
        f"{release_state} | "
        f"Publish the next approved version to GitHub and PyPI. |",
        "",
        "## Channel definitions",
        "",
        "- `bake` is where we combine and test changes planned for "
        f"{bake['version']}. When it is ready, we move it to `main` through one pull request.",
        "- `release` is the work approved for the next public version. After it reaches "
        "`main`, `tools/release.py` builds it, runs the final tests, and publishes it.",
        *active_bake_guidance,
        "",
        "## Version installed for testing",
        "",
        "| Version | Commit | Tested | Compared with current bake |",
        "|---|---|---|---|",
        f"| `{bake['deployed_version']}` | `{deployed_sha[:8]}` | "
        f"`{bake['validated_on']}` | {deployed_state} |",
        "",
        "Update this row only after that exact version has been installed and tested.",
        "",
        "## Changes moving through the channels",
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
        f"### Pull requests still in progress ({len(open_to_bake) + len(open_to_release)})",
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
        "## Work tracked in issues",
        "",
        "Open does not mean absent from bake: GitHub closes linked issues only after "
        "the work reaches the default branch.",
        "",
        "| Issue | Work | What happened in bake | GitHub state | Decision needed before release |",
        "|---|---|---|---|---|",
        *issue_rows,
        "",
        "### Recent open issues not assigned to a channel",
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
        "## What needs to happen next",
        "",
        *(f"{index}. {action}" for index, action in enumerate(next_actions, 1)),
        "",
        "## Report sources",
        "",
        f"- Release branch: `{release_sha}`",
        f"- Bake branch: `{bake_sha}`",
        f"- Latest released tag: `{latest['tagName']}` at `{tag_sha}`",
        f"- Manifest: [`.github/release-channels.toml`](../.github/release-channels.toml)",
        f"- Generator: [`tools/release-report.py`](../tools/release-report.py)",
        "",
    ])
    if as_json:
        return json.dumps({
            "schema": 1,
            "generated_at": generated_at.isoformat(),
            "repository": repository,
            "development_state": development_state,
            "development_state_detail": development_state_detail,
            "target_branch": release["branch"] if bake_moved else bake["branch"],
            "active_bake": not bake_moved,
            "release": {"branch": release["branch"], "commit": release_sha,
                        "published_tag": latest["tagName"], "published_commit": tag_sha},
            "bake": {"branch": bake["branch"], "commit": bake_sha,
                     "version": bake["version"], "promotion_approved": promotion_approved},
            "next_actions": next_actions,
        }, indent=2) + "\n"
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true", help="print structured routing to stdout")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    if args.json and args.check:
        parser.error("--json and --check are mutually exclusive")
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
    report = _render(config, generated_at, as_json=args.json)
    if args.json:
        print(report, end="")
        return 0
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != report:
            print(f"release report is stale: run {Path(__file__).name}", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8", newline="\n")
    print(f"Wrote {args.output.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as exc:
        print(f"release report: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc