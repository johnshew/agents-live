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
DEFINITION (no declaration), and the first ``start <agent> --transfer-to
<host>`` upgrades the project to registry mode itself via
``declare_ownership`` below - transferring IS the declaration of
multi-host intent. This module stays the single sanctioned mutation
point for the project config either way.

Counterpart: ``ownership.py`` is the read side of this seam - runtime
mode resolution and registry enforcement. It never writes the project
config; all config writes live here.

Unlike every other subcommand, init defines the project root rather than
requiring one: the target is --repo/AGENTS_LIVE_REPO if given, else
the current directory.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from . import paths, plugins, preflight

_DOTFILE_HEADER = (
    "# agents-live project config (and the project-root marker).\n"
    "# Managed by `agents-live init`; `ownership = \"registry\"` is\n"
    "# written only by the first --transfer-to. Do not hand-edit.\n"
)

# The skill payload init installs into a target repo (§3.4 step 2):
# docs and templates only - NO scripts/; the CLI is the executable
# surface, the skill is the thin layer that drives it.
_SKILL_PAYLOAD = ("SKILL.md", "VERSION", "docs", "templates")


def initialize(root: Path) -> bool:
    """Create the standard project layout (idempotent): the root config
    marker (``.agents-live.toml``, unless ``pyproject.toml`` already
    declares ``[tool.agents-live]``) plus ``Agents/data/`` and
    ``Agents/logs/``. Returns True if the config marker was created.
    THE single initialization code path - ``init`` runs it from the CLI
    and activate's ``--transfer-to`` bootstrap runs it before declaring
    registry mode.

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
    (agents_dir / "data").mkdir(parents=True, exist_ok=True)
    (agents_dir / "logs").mkdir(parents=True, exist_ok=True)
    (agents_dir / "handlers").mkdir(parents=True, exist_ok=True)
    return created


def _skill_source() -> Path | None:
    """Where the vendored skill payload lives: ``<module dir>/skill/``
    in the installed package (Phase 4 layout), ``<scripts>/..`` (the
    skill directory itself) in the life checkout."""
    module_dir = Path(__file__).resolve().parent
    for candidate in (module_dir / "skill", module_dir.parent):
        if (candidate / "SKILL.md").is_file():
            return candidate
    return None


def _payload_version(payload_dir: Path) -> str | None:
    """The payload's ``VERSION`` marker, or None if absent/unreadable."""
    try:
        return (payload_dir / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _copy_payload(source: Path, dest: Path) -> None:
    for item in _SKILL_PAYLOAD:
        payload = source / item
        if payload.is_dir():
            shutil.copytree(payload, dest / item)
        elif payload.is_file():
            shutil.copy2(payload, dest / item)


def _install_payload(source: Path, dest: Path) -> None:
    """Stage the payload beside *dest*, then swap it in.

    The full copy happens in a staging directory first, so a mid-copy
    failure (disk full, Ctrl-C) never destroys an existing install.
    During the swap, VERSION moves last: a payload interrupted mid-swap
    has no VERSION marker, keeps comparing as stale, and the next
    install/upgrade completes it - it can never masquerade as current.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        dir=dest.parent, prefix=".agents-live-staging-"))
    try:
        _copy_payload(source, staging)
        dest.mkdir(exist_ok=True)
        for item in _SKILL_PAYLOAD:
            target = dest / item
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        for item in sorted(_SKILL_PAYLOAD, key=lambda i: i == "VERSION"):
            staged = staging / item
            if staged.exists():
                shutil.move(str(staged), str(dest / item))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install_skill(root: Path) -> str | None:
    """Install or refresh the vendored skill payload (§3.4 step 2) in the
    target repo's ``.claude/skills/agents-live/``: SKILL.md, docs, and
    starter templates - no ``scripts/``. Returns ``"installed"`` on first
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

    The single sanctioned mutation point for the ``ownership`` key.
    Sole caller: activate's ``--transfer-to`` bootstrap (transferring IS
    the declaration of multi-host intent; there is deliberately no
    init-time flag for it). Returns True if the config changed. Raises
    ValueError if the existing config is unreadable (repair it; never
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
    (root / paths.CONFIG_DOTFILE).write_text("".join(lines), encoding="utf-8")


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

    env_root = os.environ.get(paths.ENV_VAR, "").strip()
    root = Path(env_root).resolve() if env_root else Path.cwd().resolve()

    try:
        created = initialize(root)
    except ValueError as exc:
        print(f"error [agent_invalid] init: existing project config is "
              f"malformed; repair it first: {exc}", file=sys.stderr)
        return 1
    if created:
        print(f"Initialized {paths.CONFIG_DOTFILE} (project root: {root})")
    else:
        print(f"{paths.config_source(root)} already up to date")
    try:
        if plugins.converge([root]):
            print("Converged declared plugins in the agents-live tool environment")
    except (OSError, ValueError, plugins.PluginError) as exc:
        preflight.emit_failure("init", f"plugin convergence failed: {exc}")
        return 1
    skill_status = install_skill(root)
    if skill_status == "installed":
        print("Installed skill payload: .claude/skills/agents-live/ "
              "(SKILL.md, docs, templates)")
    elif skill_status == "refreshed":
        print("Refreshed skill payload to match the installed package: "
              ".claude/skills/agents-live/")

    print(
        "\nNext steps:\n"
        "  - copy a starter from .claude/skills/agents-live/templates/\n"
        "    into Agents/<agent-name>.md and edit its frontmatter\n"
        "  - `agents-live run <agent-name>` to test it once\n"
        "  - `agents-live start <agent-name>` to activate its triggers\n"
        "  docs: https://github.com/johnshew/agents-live\n")

    # Close with a read-only doctor run (§3.4 step 6) so a fresh install
    # ends in a verified green state, not a hopeful one. Reload: doctor
    # resolves its repo root at import time, and init may target a root
    # the process hadn't resolved before.
    paths.clear_cache()
    import importlib
    from . import doctor
    doctor = importlib.reload(doctor)
    sys.argv = ["agents-live doctor"]
    print("Running doctor...")
    return doctor.main()


if __name__ == "__main__":
    raise SystemExit(main())
