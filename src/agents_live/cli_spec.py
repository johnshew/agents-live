"""Declarative description of the public agents-live command surface."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Arg:
    flags: tuple[str, ...]
    help: str
    kind: str = "flag"
    required: bool = False
    default: object = None
    choices: tuple[str, ...] = ()
    hidden: bool = False


@dataclass(frozen=True)
class Cmd:
    name: str
    summary: str
    module: str
    dispatch: str
    aliases: tuple[str, ...] = ()
    root: str = "required"
    probes: tuple[str, ...] = ()
    dynamic_probes: str | None = None
    json: bool = False
    all_repos: bool = False
    name_sugar: bool = False
    default_notice: bool = False
    update_notice: bool = True
    subcommands: tuple["Cmd", ...] = ()
    args: tuple[Arg, ...] = ()
    hidden: bool = False


GLOBAL_ARGS = (
    Arg(("--json",), "Machine-readable output and error envelopes."),
    Arg(("--repo",), "Pin a path or registered repository.", kind="value"),
    Arg(("--version",), "Show the installed version and exit."),
)


COMMANDS = (
    Cmd(
        "run", "Execute an agent once.", "run", "in-process",
        root="auto-marker", json=True, name_sugar=True, default_notice=True,
        args=(
            Arg(("--name",), "Agent name.", kind="value", required=True),
            Arg(("--changed-files",), "JSON array of changed paths.", kind="value"),
            Arg(("--quiet",), "Suppress progress output."),
        ),
    ),
    Cmd(
        "start", "Activate cron and watcher triggers.", "activate", "in-process",
        root="auto-marker", probes=("crontab", "inotify"),
        dynamic_probes="start", json=True, name_sugar=True, default_notice=True,
        args=(
            Arg(("--name",), "Agent name.", kind="value"),
            Arg(("--all",), "Activate all configured agents."),
            Arg(("--dry-run", "-n"), "Preview without mutating."),
            Arg(("--yes",), "Confirm ownership takeover without prompting."),
            Arg(("--transfer-to",), "Transfer ownership to a host.", kind="value"),
            Arg(("--prune-orphans",), "Remove triggers for deleted agents."),
            Arg(("--watch-loop",), "Run the watcher loop.", kind="value",
                hidden=True),
            Arg(("--ensure-watcher",), "Restore one watcher.", kind="value",
                hidden=True),
            Arg(("--list-reboot-watchers",), "List durable watchers.",
                hidden=True),
        ),
    ),
    Cmd(
        "stop", "Deactivate triggers and keep configuration.", "teardown",
        "in-process", aliases=("teardown",), probes=("crontab",), json=True,
        name_sugar=True, default_notice=True,
        args=(Arg(("--name",), "Agent name.", kind="value", required=True),),
    ),
    Cmd(
        "status", "List agents and runtime state.", "status", "in-process",
        json=True, all_repos=True,
        args=(
            Arg(("name",), "Optional agent name.", kind="positional"),
            Arg(("--json",), "Emit JSON."),
            Arg(("--all-repos",), "Read every registered repository."),
        ),
    ),
    Cmd(
        "logs", "Query logs and correlated event timelines.", "qlog.py",
        "subprocess",
        subcommands=(
            Cmd(
                "timeline", "Show a correlated event timeline.", "timeline.py",
                "subprocess",
                args=(
                    Arg(("filter",), "Agent or content filter.",
                        kind="positional"),
                    Arg(("--all",), "Show all agents."),
                    Arg(("--since",), "Start time.", kind="value"),
                    Arg(("--last",), "Last N events.", kind="value", default=50),
                    Arg(("--logs",), "Specific log files.", kind="value"),
                ),
            ),
        ),
        args=(
            Arg(("name",), "Agent name.", kind="positional"),
            Arg(("--log",), "Log path or glob.", kind="value"),
            Arg(("--all",), "Read all logs."),
            Arg(("--agent",), "Filter by agent.", kind="value"),
            Arg(("--since",), "Start time.", kind="value"),
            Arg(("--until",), "End time.", kind="value"),
            Arg(("--phase",), "Filter by phase.", kind="value"),
            Arg(("--status",), "Filter by status.", kind="value"),
            Arg(("--trigger",), "Filter by trigger.", kind="value"),
            Arg(("--slow",), "Minimum duration.", kind="value"),
            Arg(("--errors",), "Show errors only."),
            Arg(("-n", "--limit", "--tail"), "Maximum rows.", kind="value",
                default=200),
            Arg(("--columns",), "Columns to show.", kind="value"),
            Arg(("--order-by",), "Sort column.", kind="value"),
            Arg(("--desc",), "Sort newest first."),
            Arg(("--asc",), "Sort oldest first."),
            Arg(("--sql",), "Run custom SQL.", kind="value"),
            Arg(("--format",), "Output format.", kind="value",
                choices=("table", "jsonl", "csv"), default="table"),
            Arg(("--check-schema",), "Validate the log schema."),
        ),
    ),
    Cmd(
        "smoketest", "Run end-to-end validation.", "smoketest", "in-process",
        probes=("crontab", "inotify"), default_notice=True,
        args=(
            Arg(("--runtime",), "Agent runtime.", kind="value"),
            Arg(("--model",), "Model override.", kind="value"),
        ),
    ),
    Cmd(
        "doctor", "Check environment and installation readiness.", "prereqs",
        "in-process", aliases=("prereqs",), root="markerless", json=True,
        all_repos=True, update_notice=False,
        args=(
            Arg(("--json",), "Emit a JSON summary."),
            Arg(("--all-repos",), "Check every registered repository."),
        ),
    ),
    Cmd(
        "init", "Initialize the project layout.", "init", "in-process",
        root="none",
    ),
    Cmd(
        "upgrade", "Upgrade runtime and project skill payloads.", "upgrade",
        "in-process", root="none", update_notice=False,
        args=(
            Arg(("--runtime-only",), "Upgrade only the runtime."),
            Arg(("--skills-only",), "Refresh only skill payloads."),
        ),
    ),
    Cmd(
        "migrate", "Converge persisted runtime invocations.", "migrate",
        "in-process", probes=("crontab",), default_notice=True,
        args=(Arg(("--dry-run", "-n"), "Print the plan without mutating."),),
    ),
    Cmd(
        "heartbeat", "Run or manage the host heartbeat.", "heartbeat",
        "in-process", root="none",
        subcommands=(
            Cmd(
                "install", "Install the heartbeat.", "heartbeat", "in-process",
                root="none",
                args=(Arg(("--distro",), "Distribution name.", kind="value"),),
            ),
            Cmd(
                "uninstall", "Remove the heartbeat.", "heartbeat", "in-process",
                root="none",
                args=(
                    Arg(("--distro",), "Distribution name.", kind="value"),
                    Arg(("--retain-state",), "Retain heartbeat state."),
                ),
            ),
        ),
    ),
    Cmd(
        "uninstall", "Remove host integrations and the uv tool.", "uninstall",
        "in-process", root="none",
        args=(
            Arg(("--distro",), "Distribution name.", kind="value"),
            Arg(("--retain-state",), "Retain runtime state."),
        ),
    ),
    Cmd(
        "repos", "Manage registered repositories.", "repos", "in-process",
        root="none",
        subcommands=(
            Cmd("list", "List registered repositories.", "repos", "in-process",
                root="none"),
            Cmd(
                "add", "Register a repository.", "repos", "in-process",
                root="none",
                args=(Arg(("path",), "Repository path.", kind="positional",
                          required=True),),
            ),
            Cmd(
                "default", "Set the fallback repository.", "repos",
                "in-process", root="none",
                args=(Arg(("repo",), "Repository path or alias.",
                          kind="positional", required=True),),
            ),
            Cmd(
                "remove", "Remove a registered repository.", "repos",
                "in-process", root="none",
                args=(Arg(("repo",), "Repository path or alias.",
                          kind="positional", required=True),),
            ),
        ),
    ),
    Cmd(
        "dashboard", "Open the interactive control panel.", "dashboard.py",
        "subprocess", all_repos=True,
        args=(
            Arg(("--native",), "Open a desktop window."),
            Arg(("--open",), "Open a browser."),
            Arg(("--dev",), "Enable development mode."),
            Arg(("--port",), "Server port.", kind="value", default=8231),
            Arg(("--all-repos",), "Show every registered repository."),
        ),
    ),
)


def command_map() -> dict[str, Cmd]:
    """Return canonical commands and aliases keyed by accepted verb."""
    result: dict[str, Cmd] = {}
    for command in COMMANDS:
        result[command.name] = command
        result.update((alias, command) for alias in command.aliases)
    return result


COMMAND_BY_NAME = command_map()
