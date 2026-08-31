"""Hidden seam for building a self-managed generation without switching upgrade."""
from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from ... import deploy, plugins
from ...runtime.hosts import system as hostruntime
from ...runtime.spawn import find_uv
from ...state import registry as repos


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


def _populate(
    uv: str,
    source: Path | None,
    version: str,
    staging: Path,
    requirements: Sequence[Path] = (),
) -> None:
    _run(
        [
            uv,
            "venv",
            "--relocatable",
            "--python",
            sys.executable,
            str(staging),
        ],
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
            *(str(path) for path in requirements),
        ],
        step="installing agents-live",
    )


def _plugin_requirements() -> tuple[Path, ...]:
    roots = [Path(value) for _alias, value, error in repos.entries() if not error]
    errors = plugins.validation_errors(roots)
    if errors:
        raise deploy.generation.GenerationError("; ".join(errors))
    declarations = plugins.union(roots, require_exists=True)
    return tuple(declaration.path for declaration in declarations.values())


def install_declared_plugins(environment: Path) -> None:
    """Install every registered repository's plugin before sealing."""
    requirements = _plugin_requirements()
    if not requirements:
        return
    _run(
        [
            find_uv(), "pip", "install", "--python",
            str(_interpreter(environment)),
            *(str(path) for path in requirements),
        ],
        step="installing declared plugins",
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


def install(
    version: str,
    *,
    source: Path | None = None,
    root: Path | None = None,
    activate: bool = False,
    provenance: deploy.generation.Provenance | None = None,
) -> deploy.generation.Generation:
    """Build an exact generation through the shared uv-backed seam."""
    uv = find_uv()
    requirements = _plugin_requirements()
    built = deploy.generation.build(
        version,
        root=root,
        populate=lambda staging: _populate(
            uv, source, version, staging, requirements),
        validate=lambda staging: _validate(version, staging),
        provenance=provenance,
    )
    if activate:
        deploy.generation.activate(built, root=root)
    return built


def executable(generation: deploy.generation.Generation) -> Path:
    """Return the generation-local CLI path an operator can run immediately."""
    return (
        hostruntime.executable_dir(generation.path)
        / hostruntime.executable_filename("agents-live")
    )


def validate(generation: deploy.generation.Generation) -> None:
    """Revalidate an installed generation before it is reused or activated."""
    interpreter = _interpreter(generation.path)
    launcher = executable(generation)
    missing = [
        label for label, path in (
            ("interpreter", interpreter),
            ("launcher", launcher),
        )
        if not path.is_file()
    ]
    if missing:
        raise deploy.generation.GenerationError(
            f"generation {generation.name} is damaged: missing "
            f"{', '.join(missing)}")
    _validate(generation.name, generation.path)


def validate_environment(version: str, environment: Path) -> None:
    """Validate a dedicated environment before it is sealed as immutable."""
    launcher = (
        hostruntime.executable_dir(environment)
        / hostruntime.executable_filename("agents-live")
    )
    if not launcher.is_file():
        raise deploy.generation.GenerationError(
            f"generation {version} is damaged: missing launcher")
    _validate(version, environment)


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
        built = install(
            args.version,
            source=source,
            root=args.install_root,
            activate=args.activate,
        )
    except (FileNotFoundError, OSError, ValueError,
            deploy.generation.GenerationError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    action = "built and activated" if args.activate else "built"
    print(f"{action} generation {built.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
