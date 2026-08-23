"""Hidden seam for building a self-managed generation without switching upgrade."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from ... import deploy
from ...runtime.hosts import system as hostruntime
from ...runtime.spawn import find_uv


def _run(command: list[str], *, step: str, capture: bool = False) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=capture,
        text=capture,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() if capture else ""
        suffix = f": {detail}" if detail else f" (exit {completed.returncode})"
        raise deploy.generation.GenerationError(f"{step} failed{suffix}")
    return completed.stdout.strip() if capture else ""


def _interpreter(environment: Path) -> Path:
    return (
        hostruntime.executable_dir(environment)
        / hostruntime.executable_filename(hostruntime.interpreter_name())
    )


def _populate(uv: str, source: Path | None, version: str, staging: Path) -> None:
    _run(
        [uv, "venv", "--python", sys.executable, str(staging)],
        step="creating the generation environment",
    )
    requirement = str(source) if source is not None else f"agents-live=={version}"
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(_interpreter(staging)),
            "--reinstall-package",
            "agents-live",
            requirement,
        ],
        step="installing agents-live",
    )


def _validate(version: str, staging: Path) -> None:
    interpreter = str(_interpreter(staging))
    installed = _run(
        [
            interpreter,
            "-I",
            "-c",
            "from agents_live import __version__; print(__version__)",
        ],
        step="checking the installed package version",
        capture=True,
    )
    if installed != version:
        raise deploy.generation.GenerationError(
            f"installed package reports version {installed!r}, expected {version!r}")
    help_text = _run(
        [interpreter, "-I", "-m", "agents_live.cli", "--help"],
        step="starting the staged CLI",
        capture=True,
    )
    if "agents-live" not in help_text:
        raise deploy.generation.GenerationError(
            "staged CLI help did not identify agents-live")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and optionally activate one self-managed generation")
    parser.add_argument("version")
    parser.add_argument("--from", dest="source", type=Path)
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    source = args.source.expanduser().resolve() if args.source else None
    if source is not None and not source.exists():
        print(f"no such package source: {source}", file=sys.stderr)
        return 1
    try:
        uv = find_uv()
        built = deploy.generation.build(
            args.version,
            root=args.install_root,
            populate=lambda staging: _populate(
                uv, source, args.version, staging),
            validate=lambda staging: _validate(args.version, staging),
        )
        if args.activate:
            deploy.generation.activate(built, root=args.install_root)
    except (FileNotFoundError, OSError, ValueError,
            deploy.generation.GenerationError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    action = "built and activated" if args.activate else "built"
    print(f"{action} generation {built.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
