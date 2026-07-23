"""Shell completion scripts generated from the declarative CLI spec."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import paths, preflight
from .cli_spec import (
    COMMANDS,
    GLOBAL_ARGS,
    HELP_ARG,
    POST_COMMAND_ARGS,
    Cmd,
    visible_args,
)


def _command_words() -> list[str]:
    words: list[str] = []
    for command in COMMANDS:
        if command.hidden:
            continue
        words.append(command.name)
        words.extend(command.aliases)
    return words


def _values(command: Cmd) -> list[str]:
    values = [
        value
        for item in (command, *command.subcommands)
        if not item.hidden
        for argument in visible_args(item)
        for value in (*argument.flags, *argument.choices)
        if value.startswith("-") or value in argument.choices
    ]
    values.extend(
        flag for argument in POST_COMMAND_ARGS for flag in argument.flags)
    return list(dict.fromkeys(values))


def _agent_name_condition() -> str:
    """Shell test matching every command that accepts an agent name
    positionally (the spec's ``name_sugar`` flag), so the completion
    scripts never hardcode a verb list."""
    return " || ".join(
        f'"$command" == {command.name}'
        for command in COMMANDS
        if not command.hidden and command.name_sugar
    )


# Extracts every "name" value from the one-line JSON that
# `agents-live status --json` emits: grep -o yields one match per name
# (a line-oriented sed over single-line JSON would only ever yield the
# last agent), and the sed strips the key and quotes.
_NAMES_PIPELINE = (
    "grep -o '\"name\": *\"[^\"]*\"' |\n"
    "        sed 's/.*: *\"\\(.*\\)\"$/\\1/'"
)


def bash() -> str:
    cases = []
    for command in COMMANDS:
        if command.hidden:
            continue
        names = "|".join((command.name, *command.aliases))
        flags = " ".join((
            *(child.name for child in command.subcommands if not child.hidden),
            *_values(command),
        ))
        cases.append(f"    {names}) opts={flags!r} ;;")
    cases.append(
        "    help) opts="
        + repr(" ".join(("--all", *_command_words())))
        + " ;;"
    )
    commands = " ".join(_command_words())
    globals_text = " ".join(
        flag for argument in (*GLOBAL_ARGS, HELP_ARG)
        for flag in argument.flags)
    return f"""# bash completion for agents-live
_agents_live_agent_names() {{
    agents-live status --json 2>/dev/null |
        {_NAMES_PIPELINE}
}}

_agents_live() {{
    local cur command opts
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    command="${{COMP_WORDS[1]}}"
    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=( $(compgen -W '{commands} {globals_text}' -- "$cur") )
        return
    fi
    case "$command" in
{chr(10).join(cases)}
    esac
    if [[ {_agent_name_condition()} ]]; then
        opts="$opts $(_agents_live_agent_names)"
    fi
    COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
}}

complete -F _agents_live agents-live
"""


def zsh() -> str:
    cases = []
    for command in COMMANDS:
        if command.hidden:
            continue
        names = "|".join((command.name, *command.aliases))
        flags = " ".join((
            *(child.name for child in command.subcommands if not child.hidden),
            *_values(command),
        ))
        cases.append(f"    {names}) values=({flags}) ;;")
    cases.append(
        "    help) values=(--all " + " ".join(_command_words()) + ") ;;"
    )
    commands = " ".join(_command_words())
    globals_text = " ".join(
        flag for argument in (*GLOBAL_ARGS, HELP_ARG)
        for flag in argument.flags)
    return f"""#compdef agents-live

_agents_live_agent_names() {{
    local -a agents
    agents=("${{(@f)$(agents-live status --json 2>/dev/null |
        {_NAMES_PIPELINE})}}")
    _describe 'agent' agents
}}

_agents_live() {{
    local command
    local -a values
    command=$words[2]
    if (( CURRENT == 2 )); then
        values=({commands} {globals_text})
    else
        case "$command" in
{chr(10).join(cases)}
        esac
        if [[ {_agent_name_condition()} ]]; then
            _agents_live_agent_names
            return
        fi
    fi
    _describe 'value' values
}}

compdef _agents_live agents-live
"""


def destinations() -> tuple[Path, Path]:
    """Return the Bash and Zsh user completion destinations."""
    data_home = paths.xdg_data_home()
    return (
        data_home / "bash-completion" / "completions" / "agents-live",
        data_home / "zsh" / "site-functions" / "_agents-live",
    )


def update() -> tuple[Path, Path]:
    """Install both generated completion scripts for the current user."""
    bash_path, zsh_path = destinations()
    paths.atomic_write_text(bash_path, bash())
    paths.atomic_write_text(zsh_path, zsh())
    return bash_path, zsh_path


def remove() -> tuple[Path, ...]:
    """Remove completion files installed by agents-live."""
    removed = []
    for path in destinations():
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(path)
    return tuple(removed)


def update_best_effort(operation: str) -> bool:
    """Update completions without failing a larger lifecycle operation."""
    try:
        update()
    except OSError as exc:
        print(
            f"warning: could not update shell completions during "
            f"{operation}: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shell", nargs="?", choices=("bash", "zsh"))
    parser.add_argument(
        "--update", action="store_true",
        help="Install or refresh completions for both shells",
    )
    args = parser.parse_args()
    if args.update:
        if args.shell is not None:
            parser.error("--update cannot be combined with a shell")
        try:
            bash_path, zsh_path = update()
        except OSError as exc:
            preflight.emit_failure("completions", str(exc))
            return 1
        print(f"Installed Bash completions: {bash_path}")
        print(f"Installed Zsh completions: {zsh_path}")
        return 0
    if args.shell is None:
        parser.error("a shell or --update is required")
    print(bash() if args.shell == "bash" else zsh(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
