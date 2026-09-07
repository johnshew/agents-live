"""Hidden seam for building a self-managed generation without switching upgrade."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ... import deploy, paths, runtime
from ...runtime import handoff
from ...runtime.hosts import system as hostruntime
from ...runtime.hosts.processes import pid_exists
from ...runtime.spawn import find_uv
from ...state import registry as repos
from .. import lifecycle

WATCHER_GRACE_SECONDS = 5.0


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
    target: Path,
) -> None:
    _run(
        [
            uv,
            "venv",
            "--relocatable",
            "--python",
            sys.executable,
            str(target),
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
            str(_interpreter(target)),
            "--reinstall-package",
            "agents-live",
            requirement,
        ],
        step="installing agents-live",
    )


def _validate(version: str, environment: Path) -> None:
    interpreter = str(_interpreter(environment))
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
        step="starting the built CLI",
        capture=True,
    )
    if "agents-live" not in help_text:
        raise deploy.generation.GenerationError(
            "built CLI help did not identify agents-live")


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
    built = deploy.generation.build(
        version,
        root=root,
        populate=lambda target: _populate(
            uv, source, version, target),
        validate=lambda target: _validate(version, target),
        provenance=provenance,
    )
    if activate:
        activate_generation(built, root=root)
    return built


def activate_generation(
    generation: deploy.generation.Generation,
    *,
    root: Path | None = None,
) -> None:
    """Quiesce started intent, select a version, and restart through it."""
    install_root = root or deploy.layout.installation_root()
    previous = None
    disturbed = False
    try:
        with handoff.operation():
            try:
                with handoff.gate(), contextlib.ExitStack() as stopping:
                    installed = deploy.generation.load(generation.name, root=install_root)
                    if installed != generation:
                        raise deploy.generation.GenerationError(
                            "version changed after validation")
                    pointer, status, detail = deploy.pointer.status(
                        deploy.layout.current_path(install_root))
                    if status not in (deploy.pointer.ACTIVE, deploy.pointer.MISSING):
                        raise deploy.generation.GenerationError(detail)
                    if pointer is not None:
                        previous = deploy.generation.load(pointer.generation, root=install_root)
                    _require_idle(install_root)
                    registered = repos.load()["repos"]
                    if registered:
                        collected = lifecycle.collect(persist=False)
                        if collected.unavailable_repositories or collected.broken_definitions:
                            raise deploy.generation.GenerationError(
                                "cannot snapshot all registered agents; repair unavailable "
                                "repositories or broken definitions before activation")
                        lifecycle.collect()
                        disturbed = True
                        stopping.enter_context(handoff.pause_watchers())
                        _stop_runtime(install_root)
                    _require_idle(install_root, watchers=True)
                    disturbed = True
                    deploy.generation.activate(generation, root=install_root)
                _maintain(generation)
            except Exception as exc:
                if disturbed:
                    try:
                        with handoff.gate(), handoff.pause_watchers():
                            current, _, _ = deploy.pointer.status(
                                deploy.layout.current_path(install_root))
                            if registered and current is not None and (
                                    previous is None or current.generation != previous.name):
                                _require_idle(install_root)
                                _stop_runtime(install_root)
                            if previous is None:
                                deploy.generation.clear_activation(root=install_root)
                            else:
                                deploy.generation.activate(previous, root=install_root)
                        if previous is not None:
                            _maintain(previous)
                        elif registered:
                            result = lifecycle.converge()
                            if result.failed:
                                raise deploy.generation.GenerationError(
                                    "could not restore the previous runtime triggers")
                    except Exception as recovery:
                        raise deploy.generation.GenerationError(
                            f"activation failed: {exc}; restoration failed: {recovery}; "
                            "started intent is preserved; run versions activate again "
                            "after resolving the failure") from exc
                raise deploy.generation.GenerationError(str(exc)) from exc
    except hostruntime.LockBusy as exc:
        raise deploy.generation.GenerationError(
            "runtime activation or convergence is in progress; retry shortly") from exc


def _maintain(generation: deploy.generation.Generation) -> None:
    _run(
        [str(executable(generation)), "internal", "maintain"],
        step=f"converging generation {generation.name}",
    )


def _stop_runtime(root: Path) -> None:
    host = runtime.current()
    for trigger in host.trigger_store.list():
        host.trigger_store.remove(trigger.key)
    _require_idle(root)
    deadline = time.monotonic() + WATCHER_GRACE_SECONDS
    while host.supervisor.owned(role="watcher") and time.monotonic() < deadline:
        time.sleep(0.1)
    for process in host.supervisor.owned(role="watcher"):
        host.supervisor.terminate(process)
        if host.supervisor.alive(process):
            raise deploy.generation.GenerationError(
                f"watcher {process.pid} did not stop; activation refused")
    if host.supervisor.owned(role="watcher"):
        raise deploy.generation.GenerationError(
            "watchers are still running; activation refused")
    _require_idle(root, watchers=True)


def _require_idle(root: Path, *, watchers: bool = False) -> None:
    for lock in (paths.state_home() / "repos").glob("*/locks/*.lock"):
        try:
            document = json.loads(lock.read_text(encoding="ascii"))
            pid = int(document["pid"])
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise deploy.generation.GenerationError(
                f"cannot verify running work from {lock}; activation refused") from exc
        if pid > 0 and pid_exists(pid):
            raise deploy.generation.GenerationError(
                f"agent work is running (process {pid}); wait for it to finish "
                "and retry activation")
    processes = hostruntime.process_command_lines()
    if not any(pid == os.getpid() for pid, _command in processes):
        raise deploy.generation.GenerationError(
            "cannot verify host process inventory; activation refused")
    for pid, command in processes:
        if pid == os.getpid():
            continue
        argv = hostruntime.split_command_line(command)
        if not any(deploy.layout.generation_of(argument, root) for argument in argv[:2]):
            continue
        if "run" in argv or "maintain" in argv or (watchers and "watch-loop" in argv):
            raise deploy.generation.GenerationError(
                f"runtime process {pid} is still running; wait for it to finish "
                "and retry activation")


def executable(generation: deploy.generation.Generation) -> Path:
    """Return the generation-local CLI path an operator can run immediately."""
    return (
        hostruntime.executable_dir(generation.path)
        / hostruntime.executable_filename("agents-live")
    )


def local_provenance(source: Path) -> deploy.generation.Provenance:
    """Return stable provenance for one immutable local wheel."""
    return deploy.generation.Provenance(
        "local-artifact",
        source.name,
        hashlib.sha256(source.read_bytes()).hexdigest(),
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
            provenance=(
                local_provenance(source)
                if source is not None and source.is_file()
                else None
            ),
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
