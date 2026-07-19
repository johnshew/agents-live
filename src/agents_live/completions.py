"""Shell completion scripts generated from the declarative CLI spec."""
from __future__ import annotations

import argparse

from .cli_spec import COMMANDS, GLOBAL_ARGS, Cmd, visible_args


def _command_words() -> list[str]:
    words: list[str] = []
    for command in COMMANDS:
        if command.hidden:
            continue
        words.append(command.name)
        words.extend(command.aliases)
    return words


def _flags(command: Cmd) -> list[str]:
    return list(dict.fromkeys(
        flag
        for item in (command, *command.subcommands)
        if not item.hidden
        for argument in visible_args(item)
        for flag in argument.flags
        if flag.startswith("-")
    ))


def bash() -> str:
    cases = []
    for command in COMMANDS:
        if command.hidden:
            continue
        names = "|".join((command.name, *command.aliases))
        flags = " ".join((
            *(child.name for child in command.subcommands if not child.hidden),
            *_flags(command),
            "--help",
        ))
        cases.append(f"    {names}) opts={flags!r} ;;")
    commands = " ".join(_command_words())
    globals_text = " ".join(
        flag for argument in GLOBAL_ARGS for flag in argument.flags)
    return f"""# bash completion for agents-live
_agents_live_agent_names() {{
    agents-live status --json 2>/dev/null |
        sed -n 's/.*"name": *"\\([^"]*\\)".*/\\1/p'
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
    if [[ "$command" == run || "$command" == start ||
          "$command" == stop || "$command" == teardown ]]; then
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
            *_flags(command),
            "--help",
        ))
        cases.append(f"    {names}) values=({flags}) ;;")
    commands = " ".join(_command_words())
    globals_text = " ".join(
        flag for argument in GLOBAL_ARGS for flag in argument.flags)
    return f"""#compdef agents-live

_agents_live_agent_names() {{
    local -a agents
    agents=("${{(@f)$(agents-live status --json 2>/dev/null |
        sed -n 's/.*"name": *"\\([^"]*\\)".*/\\1/p')}}")
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
        if [[ "$command" == run || "$command" == start ||
              "$command" == stop || "$command" == teardown ]]; then
            _agents_live_agent_names
            return
        fi
    fi
    _describe 'value' values
}}

compdef _agents_live agents-live
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shell", choices=("bash", "zsh"))
    args = parser.parse_args()
    print(bash() if args.shell == "bash" else zsh(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
