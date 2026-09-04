#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# ///
"""agents-live init - project initialization (proposal §3.4, first slice).

Creates the project layout: the root config file ``.agents-live.toml``
(which is also the project marker the paths resolver walks for) plus the
``Agents/data/`` (runtime state) and ``Agents/logs/`` directories.
Idempotent - and a project whose ``pyproject.toml`` already declares a
``[tool.agents-live]`` table needs no dotfile, so none is written.
The full init (vendored skill install, templates, closing ``doctor`` run)
lands with Phase 3.

Ownership needs no init-time choice: a fresh project is local BY
DEFINITION (no declaration). ``agents-live ownership enable`` is the only
operation that adds the registry declaration through ``declare_ownership``
below. This module stays the single sanctioned mutation point for the project
config.

Counterpart: ``ownership.py`` is the read side of this seam - runtime
mode resolution and registry enforcement. It never writes the project
config; all config writes live here.

Unlike every other subcommand, init defines its target. Bare init initializes
the host-global workspace; ``init --repo`` initializes the global workspace
first and then enrolls the selected repository.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from ... import paths, plugins, preflight
from ...obs import admin as adminlog
from ...state import registry as repos
from .. import lifecycle
from . import completions

_DOTFILE_HEADER = (
    "# agents-live project config (and the project-root marker).\n"
    "# Managed by `agents-live init`; `ownership = \"registry\"` is\n"
    "# written only by `agents-live ownership enable`. Do not hand-edit.\n"
)

# The skill payload init installs into a target repo (§3.4 step 2):
# docs and templates only - NO scripts/; the CLI is the executable
# surface, the skill is the thin layer that drives it.
_SKILL_PAYLOAD = (".gitignore", "SKILL.md", "VERSION", "docs", "templates")
_SKILL_IGNORE = "*\n!.gitignore\n"


def initialize(root: Path) -> bool:
    """Create the standard project layout (idempotent): the root config
    marker (``.agents-live.toml``, unless ``pyproject.toml`` already
    declares ``[tool.agents-live]``) plus the ``Agents/`` definition root.
    Logs and other machine-local runtime state live in the user-level
    XDG state home (``paths.repo_state_dir``), never in the tree.
    Returns True if the config marker was created.
    THE single initialization code path - ``init`` runs it from the CLI.

    Reads the existing config STRICTLY first (TT-002): a malformed
    config file - including a pyproject.toml that might hold the
    ``[tool.agents-live]`` table - raises ValueError and nothing is
    written. The permissive marker probe would ignore it, and a fresh
    empty dotfile would silently shadow the repaired config (dropping a
    declared registry mode - the two-file-failure door again)."""
    paths.load_config(root)  # raises ValueError on malformed config
    created = paths.config_source(root) is None
    if created:
        (root / paths.CONFIG_DOTFILE).write_text(
            _DOTFILE_HEADER, encoding="utf-8")
    agents_dir = root / "Agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    paths.repo_state_dir(root).mkdir(parents=True, exist_ok=True)
    return created


def _skill_source() -> Path | None:
    """Where the installed package keeps its vendored skill payload."""
    candidate = Path(__file__).resolve().parents[2] / "skill"
    return candidate if (candidate / "SKILL.md").is_file() else None


def _payload_version(payload_dir: Path) -> str | None:
    """The payload's ``VERSION`` marker, or None if absent/unreadable."""
    try:
        return (payload_dir / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _copy_payload(source: Path, dest: Path) -> None:
    for item in _SKILL_PAYLOAD:
        if item == ".gitignore":
            (dest / item).write_text(_SKILL_IGNORE, encoding="utf-8")
            continue
        payload = source / item
        if payload.is_dir():
            shutil.copytree(payload, dest / item)
        elif payload.is_file():
            shutil.copy2(payload, dest / item)


def _install_payload(source: Path, dest: Path) -> None:
    """Stage the payload beside *dest*, then swap it in.

    The full copy happens in a staging directory first, so a mid-copy
    failure (disk full, Ctrl-C) never destroys an existing install. The
    existing directory is retained as a backup until the complete staged
    payload has been promoted, then restored if promotion fails.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        dir=dest.parent, prefix=".agents-live-staging-"))
    backup = dest.with_name(f".{dest.name}-backup-{uuid.uuid4().hex}")
    try:
        _copy_payload(source, staging)
        if dest.is_dir():
            for existing in dest.iterdir():
                if existing.name in _SKILL_PAYLOAD:
                    continue
                target = staging / existing.name
                if existing.is_dir():
                    shutil.copytree(existing, target, symlinks=True)
                else:
                    shutil.copy2(existing, target, follow_symlinks=False)
        if dest.exists():
            dest.rename(backup)
        try:
            staging.rename(dest)
        except BaseException:
            if backup.exists() and not dest.exists():
                backup.rename(dest)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if backup.exists() and not dest.exists():
            backup.rename(dest)


def install_skill(root: Path) -> str | None:
    """Install or refresh the vendored skill payload (§3.4 step 2) in the
    target repo's ``.claude/skills/agents-live/``: its local ignore rule,
    SKILL.md, docs, and starter templates - no ``scripts/``. Returns
    ``"installed"`` on first
    install, ``"refreshed"`` when an existing install's VERSION differed
    from the vendored payload's, and None when already current. A refresh
    replaces only the payload items; anything else in the directory (a
    source checkout's ``scripts/``, user additions) is left alone, and
    installing into the source checkout itself is a no-op."""
    source = _skill_source()
    dest = root / ".claude" / "skills" / "agents-live"
    if source is None or source.resolve() == dest.resolve():
        return None
    if not dest.exists():
        _install_payload(source, dest)
        return "installed"
    src_version = _payload_version(source)
    if src_version is None or src_version == _payload_version(dest):
        # No source VERSION to compare (flat-checkout source payloads
        # carry none - the release assembler stamps it) -> keep the old
        # leave-untouched contract rather than refreshing blindly.
        return None
    _install_payload(source, dest)
    return "refreshed"


def declare_ownership(root: Path, value: str) -> bool:
    """Write the ownership declaration into the root config dotfile.

    The single sanctioned mutation point for the ``ownership`` key, called
    only by explicit ownership enablement. Returns True if the config changed.
    Raises ValueError if the existing config is unreadable (repair it; never
    overwrite blindly).

    Always targets ``.agents-live.toml``: when the effective config
    was a pyproject table, its keys are carried into the new dotfile
    (which is authoritative from then on) so nothing is silently lost.
    """
    if value != "registry":
        # Two states only: local is the absence of the key, never a
        # written value.
        raise ValueError(f"invalid ownership mode {value!r} "
                         f"(only 'registry' is ever declared)")
    try:
        config = paths.load_config(root)
    except ValueError as exc:
        raise ValueError(f"existing project config is unreadable ({exc}); "
                         f"repair or remove it first") from exc
    if config.get("ownership") == value:
        return False
    config["ownership"] = value
    _write_dotfile(root, config)
    return True


def _write_dotfile(root: Path, config: dict) -> None:
    """Serialize *config* to ``.agents-live.toml``.

    Trivial TOML writer for the trivial schema (§3.2: no TOML-writer
    dependency): top-level strings, booleans, integers, and lists of
    strings. Anything richer is not ours to rewrite - fail loudly rather
    than corrupt it."""
    lines = [_DOTFILE_HEADER]
    for key, val in config.items():
        lines.append(f"{key} = {_toml_value(key, val)}\n")
    target = root / paths.CONFIG_DOTFILE
    descriptor, temporary = tempfile.mkstemp(
        dir=root, prefix=f".{paths.CONFIG_DOTFILE}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _toml_value(key: str, value: object) -> str:
    # json.dumps produces valid TOML basic strings (same escape rules
    # for quotes, backslashes, and control characters).
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return "[" + ", ".join(json.dumps(v) for v in value) + "]"
    if isinstance(value, dict):
        # JSON basic strings are valid quoted TOML keys, including plugin
        # distribution names containing '-' or '.'.
        return "{ " + ", ".join(
            f"{json.dumps(str(k))} = {_toml_value(f'{key}.{k}', v)}"
            for k, v in value.items()
        ) + " }"
    raise ValueError(
        f"cannot rewrite project config: key {key!r} has a value this "
        f"tool does not serialize ({type(value).__name__})")


def main() -> int:
    # No flags, by decision (2026-07-12): init initializes the standard
    # layout, installs the skill payload, and closes with a doctor run
    # (§3.4 steps 1-6). The project root comes from the CLI-global
    # --repo/AGENTS_LIVE_REPO or the current directory.
    parser = argparse.ArgumentParser(
        description="Initialize the agents-live project layout")
    parser.parse_args()

    selected_repo = os.environ.get("AGENTS_LIVE_INIT_REPO", "").strip()
    global_root = paths.global_root()
    target = Path(selected_repo).resolve() if selected_repo else None

    # Plugin convergence is the failure-prone step and needs no project
    # state, so it runs before anything is registered: a failure here has
    # nothing to undo (#226).
    try:
        global_root.mkdir(parents=True, exist_ok=True)
        plugin_roots = [global_root]
        if target is not None:
            plugin_roots.append(target)
        plugin_roots.extend(
            Path(value) for _, value, error in repos.entries() if error is None)
        # Source plugins load at runtime, so there is nothing to install
        # here. Reporting a declaration that cannot load is still worth
        # doing while the operator is looking at the command that made it.
        broken = plugins.validation_errors(list(dict.fromkeys(plugin_roots)))
        for problem in broken:
            print(f"warning: {problem}", file=sys.stderr)
    except (OSError, ValueError, plugins.PluginError) as exc:
        preflight.emit_failure("init", f"plugin declarations are invalid: {exc}")
        return 1

    try:
        global_created = initialize(global_root)
        if target is not None:
            root = target
            created = initialize(root)
            repos.ensure_default(root)
        else:
            root = global_root
            created = global_created
    except ValueError as exc:
        print(f"error [agent_invalid] init: existing project config is "
              f"malformed; repair it first: {exc}", file=sys.stderr)
        return 1
    if created:
        print(f"Initialized {paths.CONFIG_DOTFILE} (project root: {root})")
    else:
        print(f"{paths.config_source(root)} already up to date")
    adminlog.record("init", root=str(root), created=created,
                    global_created=global_created)
    global_skill_status = install_skill(global_root)
    skill_status = (
        install_skill(root) if root != global_root else global_skill_status)
    if skill_status:
        print(f"{skill_status.capitalize()} skill payload: "
              ".claude/skills/agents-live/")
    installed_completions = completions.update_best_effort("init")
    powershell_completion = next(
        (path for path in installed_completions if path.suffix == ".ps1"), None)
    if powershell_completion is not None:
        quoted = str(powershell_completion).replace("'", "''")
        print(
            "\nPowerShell completion installed: "
            f"{powershell_completion}\n"
            "Add this line to $PROFILE, then open a new PowerShell session:\n"
            f"  . '{quoted}'"
        )
    try:
        convergence = lifecycle.converge()
    except lifecycle.CollectionUnavailable as exc:
        preflight.emit_failure("init", str(exc))
        return 1
    if convergence.failed:
        for operation, detail in convergence.failed:
            print(f"{operation.key}: {detail}", file=sys.stderr)
        return 1
    print(
        "\nNext steps:\n"
        "  - create Agents/<agent-name>/SKILL.md\n"
        "  - `agents-live run <agent-name>` to test it once\n"
        "  - `agents-live start <agent-name>` to start automatic runs\n"
        "  docs: https://github.com/johnshew/agents-live\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
