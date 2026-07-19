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
        "stop", "Deactivate triggers and keep configuration.", "stop",
        "in-process", probes=("crontab",), json=True, name_sugar=True,
        default_notice=True,
        args=(Arg(("--name",), "Agent name.", kind="value", required=True),),
    ),
    Cmd(
        "status", "List agents and runtime state.", "status", "in-process",
        json=True, all_repos=True,
        args=(
            Arg(("name",), "Optional agent name.", kind="positional"),
            Arg(("--all-repos",), "Read every registered repository."),
        ),
    ),
    Cmd(
        "logs", "Query logs and correlated event timelines.", "qlog.py",
        "subprocess", json=True,
        subcommands=(
            Cmd(
                "timeline", "Show a correlated event timeline.", "timeline.py",
                "subprocess", json=True,
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
        probes=("crontab", "inotify"), json=True, default_notice=True,
        args=(
            Arg(("--runtime",), "Agent runtime.", kind="value"),
            Arg(("--model",), "Model override.", kind="value"),
        ),
    ),
    Cmd(
        "doctor", "Check environment and installation readiness.", "doctor",
        "in-process", root="markerless", json=True,
        all_repos=True, update_notice=False,
        args=(
            Arg(("--all-repos",), "Check every registered repository."),
        ),
    ),
    Cmd(
        "init", "Initialize the project layout.", "init", "in-process",
        root="none", json=True,
    ),
    Cmd(
        "upgrade", "Upgrade runtime and project skill payloads.", "upgrade",
        "in-process", root="none", json=True, update_notice=False,
        args=(
            Arg(("--runtime-only",), "Upgrade only the runtime."),
            Arg(("--skills-only",), "Refresh only skill payloads."),
        ),
    ),
    Cmd(
        "migrate", "Converge persisted runtime invocations.", "migrate",
        "in-process", probes=("crontab",), json=True, default_notice=True,
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
        root="none", json=True,
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
        "completions", "Generate shell completion scripts.", "completions",
        "in-process", root="none",
        args=(
            Arg(("shell",), "Shell name.", kind="positional", required=True,
                choices=("bash", "zsh")),
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


def visible_args(command: Cmd) -> tuple[Arg, ...]:
    return tuple(argument for argument in command.args if not argument.hidden)


def command_help(command: Cmd, invoked_as: str | None = None) -> str:
    """Render compact per-command help from the command spec."""
    name = invoked_as or command.name
    usage_parts = [f"usage: agents-live {name}"]
    if command.subcommands:
        usage_parts.append(
            "{" + ",".join(sub.name for sub in command.subcommands
                           if not sub.hidden) + "}")
    if visible_args(command):
        usage_parts.append("[options]")
    lines = [" ".join(usage_parts), "", command.summary]
    arguments = visible_args(command)
    if arguments:
        lines.extend(("", "arguments:"))
        for argument in arguments:
            flags = ", ".join(argument.flags)
            lines.append(f"  {flags:<24} {argument.help}")
    return "\n".join(lines) + "\n"


def render_usage(version: str, docs_url: str) -> str:
    """Render top-level usage and links from the public command spec."""
    commands = []
    for command in COMMANDS:
        if command.hidden:
            continue
        name = command.name
        if command.aliases:
            name += f" ({', '.join(command.aliases)})"
        commands.append(f"  {name:<24} {command.summary}")
    globals_text = [
        f"  {', '.join(argument.flags):<24} {argument.help}"
        for argument in GLOBAL_ARGS
    ]
    blob = f"{docs_url}/blob/v{version}/src/agents_live/skill/docs"
    return "\n".join([
        "usage: agents-live [--json] [--repo PATH] <command> [args]",
        "       agents-live --version",
        "",
        "commands:",
        *commands,
        "",
        "global flags:",
        *globals_text,
        "",
        f"docs: {docs_url}",
        f"  commands reference  {blob}/commands.md",
        f"  CLI grammar         {blob}/commands.md#cli-grammar",
        f"  architecture        {blob}/approach.md",
        f"  diagnostics         {blob}/diagnostics.md",
        "",
    ])


def _ebnf_flags(command: Cmd, *, skip: frozenset[str] = frozenset()) -> str:
    parts: list[str] = []
    for argument in visible_args(command):
        if any(flag in skip for flag in argument.flags):
            continue
        if argument.kind == "positional":
            if argument.choices:
                token = "( " + " | ".join(
                    f'"{choice}"' for choice in argument.choices) + " )"
            else:
                token = argument.flags[0].upper()
            parts.append(token if argument.required else f"[ {token} ]")
            continue
        flags = " | ".join(f'"{flag}"' for flag in argument.flags)
        if len(argument.flags) > 1:
            flags = f"( {flags} )"
        if argument.kind == "value":
            value = (
                "( " + " | ".join(f'"{choice}"'
                                   for choice in argument.choices) + " )"
                if argument.choices else "VALUE"
            )
            flags += f" {value}"
        parts.append(f"[ {flags} ]")
    return " ".join(parts)


def _ebnf_command(command: Cmd) -> list[str]:
    prefix = f'{command.name:<12} ::= "{command.name}"'
    if command.name_sugar:
        name_arg = next(
            argument for argument in command.args
            if "--name" in argument.flags)
        alternatives = ['NAME', '"--name" NAME']
        has_all = any("--all" in argument.flags for argument in command.args)
        if has_all:
            alternatives.append('"--all"')
        selector = "( " + " | ".join(alternatives) + " )"
        if not name_arg.required and not has_all:
            selector = f"[ {selector} ]"
        suffix = _ebnf_flags(
            command, skip=frozenset({"--name", "--all"}))
        return [f"{prefix} {selector}" + (f" {suffix}" if suffix else "")]
    if command.name == "logs":
        query = _ebnf_flags(command)
        lines = [f'{prefix} ( query | "timeline" timeline_args )']
        lines.append(f"query        ::= {query}")
        timeline = command.subcommands[0]
        lines.append(f"timeline_args ::= {_ebnf_flags(timeline)}")
        return lines
    if command.subcommands:
        alternatives = []
        for child in command.subcommands:
            if child.hidden:
                continue
            suffix = _ebnf_flags(child)
            alternatives.append(
                f'"{child.name}"' + (f" {suffix}" if suffix else ""))
        group = "( " + " | ".join(alternatives) + " )"
        if command.name == "heartbeat":
            group = f"[ {group} ]"
        return [f"{prefix} {group}"]
    suffix = _ebnf_flags(command)
    return [prefix + (f" {suffix}" if suffix else "")]


def render_grammar() -> str:
    """Render the non-hidden command surface as EBNF."""
    public = [command for command in COMMANDS if not command.hidden]
    names = " | ".join(command.name for command in public)
    lines = [
        'invocation   ::= "agents-live" pre_command*'
        ' ( command post_command* | help_word )',
        'help_word    ::= "-h" | "--help" | "help" | "--version" | ""',
        'pre_command  ::= "--json" | "--repo" ( PATH | ALIAS )',
        'post_command ::= "--json"',
        f"command      ::= {names}",
    ]
    for command in public:
        lines.extend(_ebnf_command(command))
    return "\n".join(lines)


def render_command_table() -> str:
    """Render the command policy and flag table for the reference docs."""
    header = (
        "| command | dispatch | root | probes | JSON | all repos | "
        "name sugar | flags | summary |\n"
        "|---|---|---|---|---|---|---|---|---|"
    )
    rows = [header]
    for command in COMMANDS:
        if command.hidden:
            continue
        name = command.name
        if command.aliases:
            name += f" (aliases: {', '.join(command.aliases)})"
        flags = ", ".join(
            flag for argument in visible_args(command)
            for flag in argument.flags if flag.startswith("-")
        ) or ""
        rows.append(
            f"| {name} | {command.dispatch} | {command.root} | "
            f"{', '.join(command.probes)} | "
            f"{'yes' if command.json else ''} | "
            f"{'yes' if command.all_repos else ''} | "
            f"{'yes' if command.name_sugar else ''} | {flags} | "
            f"{command.summary} |"
        )
        for child in command.subcommands:
            if child.hidden:
                continue
            child_flags = ", ".join(
                flag for argument in visible_args(child)
                for flag in argument.flags if flag.startswith("-")
            )
            rows.append(
                f"| {command.name} {child.name} | {child.dispatch} | "
                f"{child.root} | {', '.join(child.probes)} | "
                f"{'yes' if child.json else ''} | "
                f"{'yes' if child.all_repos else ''} |  | {child_flags} | "
                f"{child.summary} |"
            )
    return "\n".join(rows)


def render_docs_block() -> str:
    """Render the exact generated block embedded in commands.md."""
    return "\n".join([
        "<!-- BEGIN GENERATED CLI -->",
        "## CLI grammar",
        "",
        "The public command surface is generated from the declarative command",
        "spec. `VALUE`, `NAME`, `PATH`, and `ALIAS` are terminal values.",
        "",
        "```ebnf",
        render_grammar(),
        "```",
        "",
        "## CLI command and flag table",
        "",
        render_command_table(),
        "<!-- END GENERATED CLI -->",
    ])


def unknown_flag(command: Cmd, argv: list[str]) -> str | None:
    """Return the first option not declared for a command or subcommand."""
    current = command
    if argv:
        child = next(
            (item for item in command.subcommands if item.name == argv[0]),
            None,
        )
        if child is not None:
            current = child
            argv = argv[1:]
    arguments = (*command.args, *(current.args if current is not command else ()))
    known = {flag for argument in arguments for flag in argument.flags
             if flag.startswith("-")}
    known.add("--all-repos")
    takes_value = {
        flag for argument in arguments if argument.kind == "value"
        for flag in argument.flags if flag.startswith("-")
    }
    skip_value = False
    for token in argv:
        if skip_value:
            skip_value = False
            continue
        option = token.split("=", 1)[0]
        if option.startswith("-") and option not in known:
            return option
        if option in takes_value and "=" not in token:
            skip_value = True
    return None


def validation_error(command: Cmd, argv: list[str]) -> str | None:
    """Return a concise spec-derived usage error, or None."""
    if command.name == "start":
        if "--yes" in argv and "--all" in argv:
            return "--yes requires a targeted name and cannot be used with --all"
        if "--all" not in argv and "--name" not in argv and (
                not argv or argv[0].startswith("-")):
            return "start requires NAME, --name NAME, or --all"
    if command.name == "upgrade":
        if "--runtime-only" in argv and "--skills-only" in argv:
            return "--runtime-only and --skills-only are mutually exclusive"
    current = command
    if command.subcommands and argv:
        child = next(
            (item for item in command.subcommands if item.name == argv[0]),
            None,
        )
        if child is not None:
            current = child
            argv = argv[1:]
        elif command.name == "repos":
            return "repos requires one of: " + ", ".join(
                item.name for item in command.subcommands if not item.hidden)
    elif command.name == "repos":
        return "repos requires one of: " + ", ".join(
            item.name for item in command.subcommands if not item.hidden)
    for argument in current.args:
        if argument.hidden:
            continue
        if argument.kind == "positional":
            values = [value for value in argv if not value.startswith("-")]
            if argument.required and not values:
                return f"{argument.flags[0]} is required"
            if values and argument.choices and values[0] not in argument.choices:
                return (
                    f"{argument.flags[0]} must be one of: "
                    + ", ".join(argument.choices)
                )
        elif argument.kind == "value":
            for flag in argument.flags:
                if flag not in argv:
                    continue
                index = argv.index(flag)
                if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                    return f"{flag} requires a value"
            if argument.required and not any(
                    flag in argv for flag in argument.flags):
                if not command.name_sugar or not argv or argv[0].startswith("-"):
                    return f"{argument.flags[0]} is required"
    return None
