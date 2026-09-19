#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Report every configured release cycle and all open repository work."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import runpy
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".github" / "release-cycles.toml"
OUTPUT = ROOT / ".reports" / "release-report.md"


class ReportError(RuntimeError):
    pass


def _run(*argv: str) -> str:
    completed = subprocess.run(
        argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False)
    if completed.returncode:
        raise ReportError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def _json(*argv: str) -> Any:
    return json.loads(_run(*argv))


def _sha(ref: str) -> str:
    return _run("git", "rev-parse", f"{ref}^{{commit}}")


def _retained_inventory() -> dict:
    common = Path(_run("git", "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = ROOT / common
    local = []
    for directory in sorted((common / "agents-live-local-deploy" / "candidates").glob("*")):
        row = {"attempt": directory.name, "state": "incomplete"}
        try:
            receipt = directory / "receipt.json"
            prepared = directory / "preparation.json"
            artifact = directory / "artifact.json"
            source = receipt if receipt.exists() else prepared if prepared.exists() else artifact
            payload = json.loads(source.read_text(encoding="utf-8"))
            wheel = Path(payload["wheel"])
            with wheel.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            valid = payload["version"] == directory.name and digest == payload["wheel_sha256"]
            row.update(state=("local-deployed" if payload.get("deployed") else
                              "local-prepared" if payload.get("prepared") else "artifact-only")
                       if valid else "invalid-evidence",
                       source_commit=payload.get("commit"), wheel_sha256=digest)
        except (OSError, ValueError, KeyError, TypeError):
            row["state"] = "invalid-evidence"
        local.append(row)
    retained = [directory.name.removeprefix("cycle-") for directory in
                sorted((common / "agents-live-release").glob("cycle-*")) if directory.is_dir()]
    return {"local_candidates": local, "retained_cycles": retained}


def _link(repository: str, kind: str, number: int) -> str:
    return f"[#{number}](https://github.com/{repository}/{kind}/{number})"


def _checks(pr: dict) -> str:
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return "not reported"
    if any(item.get("status", "COMPLETED") != "COMPLETED" for item in checks):
        return "pending"
    if any(item.get("conclusion", item.get("state")) not in
           {"SUCCESS", "NEUTRAL", "SKIPPED"} for item in checks):
        return "failed"
    return "passed"


def _local_attempts(cycle: dict) -> list[dict]:
    release_tool = runpy.run_path(str(ROOT / "tools" / "release.py"))
    release_tool["cycle_status"].__globals__["_cycle_configuration"] = lambda: cycle
    with contextlib.redirect_stdout(sys.stderr):
        return release_tool["cycle_status"]()


def _render(config: dict, generated_at: datetime, *, as_json: bool = False) -> str:
    if config.get("schema") != 2 or not config.get("cycles"):
        raise ReportError("expected release cycles manifest schema 2")
    repository_data = _json("gh", "repo", "view", "--json", "nameWithOwner,url")
    repository = repository_data["nameWithOwner"]
    releases = _json("gh", "release", "list", "--limit", "1000", "--json",
                     "tagName,isDraft,isPrerelease,publishedAt")
    issues = _json("gh", "issue", "list", "--state", "open", "--limit", "1000",
                   "--json", "number,title,state,url,updatedAt")
    prs = _json("gh", "pr", "list", "--state", "open", "--limit", "1000", "--json",
                "number,title,baseRefName,headRefName,isDraft,statusCheckRollup,reviewDecision,url")
    if any(len(items) >= 1000 for items in (releases, issues, prs)):
        raise ReportError("GitHub result limit reached; report would be incomplete")
    stable = [item for item in releases if not item["isDraft"] and not item["isPrerelease"]]
    latest = stable[0]["tagName"] if stable else None
    try:
        installed = _run("agents-live", "--version")
    except (OSError, ReportError) as exc:
        installed = f"unavailable: {exc}"
    cycles = {}
    inventory = _retained_inventory()
    actions = []
    assigned = set()
    for version, configured in config["cycles"].items():
        if re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
            raise ReportError(f"invalid stable target: {version}")
        cycle = dict(configured, version=version)
        branch = cycle["branch"]
        source_ref = cycle.get("source_ref", f"origin/{branch}")
        source = _sha(source_ref)
        attempts = _local_attempts(cycle)
        selected = cycle.get("selected_rc")
        selected_attempt = next((item for item in attempts if item["attempt"] == selected), None)
        approval = cycle.get("approval", {})
        approved_source = approval.get("commit")
        approval_valid = approval.get("decision") == "approved" and bool(approved_source)
        try:
            datetime.strptime(approval.get("decided_on", ""), "%Y-%m-%d")
        except (TypeError, ValueError):
            approval_valid = False
        if selected_attempt and selected_attempt.get("source_commit") != approved_source:
            approval_valid = False
        selected_source = (selected_attempt or {}).get("source_commit")
        source_changes = _run("git", "log", "--format=%h %s", f"{selected_source}..{source}").splitlines() if selected_source else []
        published = any(item["tagName"] == f"v{version}" for item in stable)
        final_attempts = [item for item in attempts if "-final-" in item["attempt"]]
        final = max(final_attempts, key=lambda item: int(item["attempt"].rsplit("-", 1)[1]),
                    default=None)
        state = "published" if published else "developing"
        if not published:
            if final:
                state = f"final {final['state']}"
                operation = {"reserved": "prepare-attempt", "prepared": "accept-candidate",
                             "accepted": "finalize", "finalized": "publish"}.get(final["state"])
                action = (f"Continue {final['attempt']}: {operation}; publish only accepted packages."
                          if operation else f"Resolve {final['attempt']} evidence before publication.")
            elif selected:
                state = f"RC {selected_attempt['state']}" if selected_attempt else "RC evidence missing"
                action = (f"Prepare final {version} from accepted {selected}."
                          if selected_attempt and selected_attempt["state"] == "accepted" and approval_valid
                          else f"Obtain exact-source publication approval for {selected}."
                          if selected_attempt and selected_attempt["state"] == "accepted"
                          else f"Complete exact-package acceptance of selected {selected} for {version}.")
            else:
                action = f"Finish planned {version} work, then prepare and activate {cycle['next_rc']}."
            actions.append(action)
        rows = []
        for disposition, numbers in cycle.get("issues", {}).items():
            for number in numbers:
                assigned.add(number)
                issue = next((item for item in issues if item["number"] == number), None)
                if issue is None:
                    issue = _json("gh", "issue", "view", str(number), "--json", "number,title,state,url")
                rows.append(dict(issue, disposition=disposition))
        cycles[version] = dict(cycle, commit=source, state=state, attempts=attempts,
                               approval_valid=approval_valid, source_changes=source_changes,
                               work=rows, github_published=published,
                               pypi_status="not-verified")
    unassigned = [item for item in issues if item["number"] not in assigned]
    payload = {"schema": 2, "generated_at": generated_at.isoformat(),
               "repository": repository, "target_branch": config["development_branch"],
               "latest_stable_tag": latest, "selected_runtime": installed,
               "cycles": cycles, "open_pull_requests": prs, "unassigned_issues": unassigned,
               "local_candidates": inventory["local_candidates"],
               "unconfigured_retained_cycles": [version for version in inventory["retained_cycles"] if version not in cycles],
               "next_actions": actions}
    if as_json:
        return json.dumps(payload, indent=2) + "\n"
    lines = ["# Release Report", "",
             f"Point-in-time report for {repository}, generated at `{generated_at.isoformat()}`.",
             "", f"Latest GitHub stable release: `{latest or 'none'}`. PyPI availability is not independently verified.",
             f"Local selection: {installed}.", "",
             "Local activation is part of RC testing. Installation is not publication approval.",
             "", "## Release Cycles", "",
             "| Target | Source branch | Source commit | Selected RC | Next RC | State |",
             "|---|---|---|---|---|---|"]
    for version, cycle in cycles.items():
        lines.append(f"| {version} | `{cycle['branch']}` | `{cycle['commit']}` | "
                     f"{cycle.get('selected_rc', '-')} | {cycle.get('next_rc', '-')} | {cycle['state']} |")
    for version, cycle in cycles.items():
        lines.extend(["", f"## {version}", "", cycle.get("recommendation", ""), "",
                      "### Candidates and Final Packages", "",
                      "| Attempt | Local evidence |", "|---|---|"])
        for attempt in cycle["attempts"]:
            lines.append(f"| {attempt['attempt']} | {attempt['state']} |")
        if not cycle["attempts"]:
            lines.append("| - | No retained lifecycle attempts |")
        for identifier, record in cycle.get("history", {}).items():
            lines.append(f"| {identifier} (historical) | {record['status']}: {record['evidence']} |")
        lines.extend(["", f"Exact-source publication approval verified: {cycle['approval_valid']}.",
                      "", "Source commits after the selected RC:",
                      *(f"- {commit}" for commit in cycle["source_changes"])])
        if not cycle["source_changes"]:
            lines.append("None, or selected RC source evidence is unavailable.")
        observation = cycle.get("deployment", {})
        if observation:
            lines.extend(["", f"Last recorded deployment: {observation.get('version', 'unknown')} "
                          f"on {observation.get('validated_on', 'unknown')}. "
                          f"{observation.get('evidence', '')}"])
        lines.extend(["", "### Scope", "", "| Issue | Work | Decision | GitHub state |",
                      "|---|---|---|---|"])
        for issue in cycle["work"]:
            lines.append(f"| {_link(repository, 'issues', issue['number'])} | {issue['title']} | "
                         f"{issue['disposition']} | {issue['state']} |")
        for number, decision in cycle.get("decisions", {}).items():
            lines.append(f"\n- {_link(repository, 'issues', int(number))}: {decision}")
    lines.extend(["", "## All Open Pull Requests", "",
                  "| PR | Change | From | To | Checks | Review |", "|---|---|---|---|---|---|"])
    for pr in prs:
        lines.append(f"| {_link(repository, 'pull', pr['number'])} | {pr['title']} | "
                     f"{pr['headRefName']} | {pr['baseRefName']} | {_checks(pr)} | {pr.get('reviewDecision') or 'not reported'} |")
    if not prs:
        lines.append("| - | None | - | - | - | - |")
    lines.extend(["", "## Historical Local Candidate Evidence", "",
                  "These limited receipts do not replace full package acceptance."])
    lines.extend(f"- {row['attempt']}: {row['state']}" for row in inventory["local_candidates"])
    if not inventory["local_candidates"]:
        lines.append("None.")
    lines.extend(["", "Unconfigured retained cycles: " +
                  (", ".join(payload["unconfigured_retained_cycles"]) or "none") + "."])
    lines.extend(["", "## Open Issues Outside Configured Cycles", ""])
    lines.extend(f"- {_link(repository, 'issues', item['number'])}: {item['title']}" for item in unassigned)
    if not unassigned:
        lines.append("None.")
    lines.extend(["", "## Next Actions", "",
                  *(f"{index}. {action}" for index, action in enumerate(actions, 1)), "",
                  "Use a clean checkout or isolated worktree; verify source ancestry before committing or pushing.",
                  "Remove task worktrees after delivery, preserving retained candidate worktrees and receipts.",
                  "Retain immutable packages and receipts. Restore dashboards and watchers after activation.", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    if args.json and args.check:
        parser.error("--json and --check are mutually exclusive")
    with CONFIG.open("rb") as stream:
        config = tomllib.load(stream)
    generated_at = datetime.now(timezone.utc)
    if args.check and args.output.exists():
        current = args.output.read_text(encoding="utf-8")
        match = re.search(r"generated at `([^`]+)`", current)
        if match is None:
            raise ReportError("report has no generation timestamp")
        generated_at = datetime.fromisoformat(match.group(1))
    report = _render(config, generated_at, as_json=args.json)
    if args.json:
        print(report, end="")
    elif args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != report:
            raise ReportError("release report is stale")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8", newline="\n")
        print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReportError, OSError, ValueError) as exc:
        print(f"release report: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc