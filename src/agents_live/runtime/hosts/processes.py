"""Subprocess and detached-process host capabilities."""
from __future__ import annotations

import os
import signal
import shlex
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO

from ..values import ChildResult, ProcessRef
from . import system


def pid_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() != 87
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def within(candidate: str, root: Path | str) -> bool:
    """Whether a command-line argument names something inside *root*."""
    return bool(candidate) and Path(root) in Path(candidate).parents


def watchers_on_host(
    *, under: Path | None = None,
) -> list[tuple[int, str, str | None]]:
    """Return running Agents Live watchers as ``(pid, agent, project)``."""
    found: list[tuple[int, str, str | None]] = []
    for pid, command in system.process_command_lines():
        args = system.split_command_line(command)
        if not any(
            "activate.py" in argument or Path(argument).stem == "agents-live"
            for argument in args
        ):
            continue
        if under is not None and not any(
            within(argument, under) for argument in args):
            continue
        name = next(
            (
                second
                for first, second in zip(args, args[1:])
                if first in ("watch-loop", "--watch-loop")
            ),
            None,
        )
        if not name:
            continue
        project = next(
            (
                second
                for first, second in zip(args, args[1:])
                if first == "--repo"
            ),
            None,
        )
        found.append((pid, name, project))
    return found


class LocalProcesses:
    def spawn_detached(
        self,
        argv: Sequence[str],
        *,
        role: str,
        key: str = "",
        fingerprint: str = "",
        cwd: str | None = None,
        stdout: IO[bytes] | int | None = None,
        stderr: IO[bytes] | int | None = None,
    ) -> ProcessRef:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.DEVNULL,
            stderr=stderr if stderr is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
        return ProcessRef(
            process.pid,
            time.time(),
            Path(argv[0]).name,
            role,
            key,
            fingerprint,
        )

    def alive(self, ref: ProcessRef) -> bool:
        try:
            os.kill(ref.pid, 0)
        except OSError:
            return False
        return True

    def adopt(
        self, pid: int, *, role: str, key: str = "",
        fingerprint: str = "", image: str = "",
    ) -> ProcessRef:
        return ProcessRef(pid, time.time(), image, role, key, fingerprint)

    def defer_until_environment_exits(self, *_args, **_kwargs) -> None:
        return None

    def terminate(self, ref: ProcessRef) -> None:
        if not self.alive(ref):
            return
        os.kill(ref.pid, signal.SIGTERM)

    def owned(self, role: str | None = None) -> list[ProcessRef]:
        proc = Path("/proc")
        if not proc.is_dir():
            return []
        found: list[ProcessRef] = []
        for item in proc.iterdir():
            if not item.name.isdigit():
                continue
            try:
                argv = (item / "cmdline").read_bytes().decode(
                    errors="replace").split("\0")
                stat = (item / "stat").read_text(encoding="utf-8").split()
            except OSError:
                continue
            parsed = _markers(argv)
            if parsed is None or (role is not None and parsed["role"] != role):
                continue
            found.append(ProcessRef(
                int(item.name),
                float(stat[21]) if len(stat) > 21 else 0.0,
                Path(argv[0]).name,
                parsed["role"],
                parsed["key"],
                parsed["fingerprint"],
            ))
        return found


class LocalChildRunner:
    def run_child(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
        use_pty: bool = False,
    ) -> ChildResult:
        argv = _shell_processor_argv(argv)
        overflow = system.command_line_overflow(argv)
        if overflow is not None:
            # Reported as a failed child rather than raised, so it travels
            # the path every other launch failure takes. The prompt is the
            # only argument that grows without bound, so it is named.
            prompt = max((str(part) for part in argv), key=len, default="")
            return ChildResult(
                tuple(argv),
                -1,
                "",
                f"prompt too large: the longest argument is {len(prompt)} "
                f"characters, putting this host's command line {overflow} "
                "characters over its limit. Windows caps a command line at "
                "32767 characters and reports the overflow as 'the filename "
                "or extension is too long'. Shorten the definition, the "
                "instructions passed with --prompt, or what the pre-processor "
                "passes through the prompt.",
            )
        if use_pty and os.name != "nt":
            return self._run_pty(
                argv, cwd=cwd, env=env, input_text=input_text, timeout=timeout)
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                input=input_text,
                stdin=subprocess.DEVNULL if input_text is None else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ChildResult(
                tuple(argv),
                -1,
                _text(exc.stdout),
                _text(exc.stderr),
                True,
            )
        return ChildResult(
            tuple(argv),
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )

    def _run_pty(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        input_text: str | None,
        timeout: float | None,
    ) -> ChildResult:
        command = shlex.join(argv)
        with tempfile.NamedTemporaryFile(
            prefix="agents-live-pty-", delete=False
        ) as handle:
            transcript = Path(handle.name)
        try:
            try:
                completed = subprocess.run(
                    ["script", "-qec", command, str(transcript)],
                    cwd=cwd,
                    env=env,
                    input=input_text,
                    stdin=subprocess.DEVNULL if input_text is None else None,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                return ChildResult(
                    tuple(argv), -1, _text(exc.stdout), _text(exc.stderr), True)
            try:
                output = transcript.read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                output = completed.stdout
            return ChildResult(
                tuple(argv),
                completed.returncode,
                output.replace("\r", ""),
                completed.stderr,
            )
        finally:
            transcript.unlink(missing_ok=True)


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _shell_processor_argv(argv: Sequence[str]) -> tuple[str, ...]:
    invocation = tuple(argv)
    if len(invocation) != 1 or Path(invocation[0]).suffix.lower() != ".sh":
        return invocation
    if os.name == "nt":
        return ("sh", invocation[0])
    if not os.access(invocation[0], os.X_OK):
        raise ValueError(f"shell processor is not executable: {invocation[0]}")
    return invocation


def _markers(argv: Sequence[str]) -> dict[str, str] | None:
    from .. import artifacts
    metadata = artifacts.from_argv(argv)
    if metadata is None or "watch-loop" not in argv:
        return None
    return {
        "role": "watcher",
        "key": metadata.id,
        "fingerprint": artifacts.PREFIX + metadata.id,
    }
