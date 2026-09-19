"""Subprocess and detached-process host capabilities."""
from __future__ import annotations

import os
import shlex
import subprocess
import threading
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
        name = _watcher_name(args)
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


def _watcher_name(args: Sequence[str]) -> str | None:
    from .. import artifacts

    metadata = artifacts.from_argv(args)
    if metadata is not None and metadata.target.startswith("agent:"):
        return metadata.target.removeprefix("agent:") or None
    return next(
            (
                second
                for first, second in zip(args, args[1:])
                if first in ("watch-loop", "--watch-loop")
            ),
            None,
        )


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
        process = system.spawn_detached(
            argv,
            cwd=cwd,
            stdout=stdout if stdout is not None else subprocess.DEVNULL,
            stderr=stderr if stderr is not None else subprocess.DEVNULL,
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

    def terminate(self, ref: ProcessRef) -> None:
        if not self.alive(ref):
            return
        system.terminate(ref.pid)

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
    diagnostic_limit = 64 * 1024 * 1024

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
        started = time.monotonic()
        limited = threading.Event()
        capture_failed = threading.Event()
        capture_errors = []
        buffers = [bytearray(), bytearray()]
        process = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=0x00000004 if os.name == "nt" else 0,
        )
        terminate_child = system.supervise_child(process)

        capture_prefix = (env or {}).get("AGENTS_LIVE_CAPTURE_PREFIX")

        def collect(stream, buffer, suffix):
            snapshot = None
            try:
                if capture_prefix:
                    descriptor = os.open(capture_prefix + suffix, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    snapshot = os.fdopen(descriptor, "wb")
                while chunk := stream.read1(65536):
                    remaining = self.diagnostic_limit - len(buffer)
                    buffer.extend(chunk[:remaining])
                    if snapshot is not None:
                        snapshot.write(chunk[:remaining])
                        snapshot.flush()
                    if len(chunk) > remaining:
                        limited.set()
                        return
            except OSError as exc:
                capture_errors.append(exc)
                capture_failed.set()
            finally:
                stream.close()
                if snapshot is not None:
                    snapshot.close()

        def feed():
            try:
                process.stdin.write(input_text.encode("utf-8"))
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()

        workers = [threading.Thread(target=collect, args=(stream, buffer, suffix), daemon=True)
               for stream, buffer, suffix in zip((process.stdout, process.stderr), buffers,
                                 (".stdout", ".stderr"))]
        if input_text is not None:
            workers.append(threading.Thread(target=feed, daemon=True))
        for worker in workers:
            worker.start()
        timed_out = False
        cleanup_s = 0.0

        try:
            while process.poll() is None or any(worker.is_alive() for worker in workers):
                timed_out = timeout is not None and time.monotonic() - started >= timeout
                if timed_out or limited.is_set() or capture_failed.is_set():
                    cleanup_started = time.monotonic()
                    terminate_child()
                    process.wait(timeout=5)
                    cleanup_deadline = time.monotonic() + 5
                    for worker in workers:
                        worker.join(timeout=max(0, cleanup_deadline - time.monotonic()))
                    cleanup_s = time.monotonic() - cleanup_started
                    if any(worker.is_alive() for worker in workers):
                        raise RuntimeError("child stream cleanup did not complete")
                    break
                limited.wait(0.02)
        finally:
            terminate_child()
            if process.poll() is None:
                process.wait(timeout=5)
        if capture_errors:
            raise RuntimeError("child diagnostic capture failed") from capture_errors[0]
        return ChildResult(
            tuple(argv), process.returncode,
            _text(bytes(buffers[0])), _text(bytes(buffers[1])), timed_out,
            limited.is_set(), cleanup_s,
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
        from dataclasses import replace

        result = self.run_child(
            ["script", "-qec", shlex.join(argv), os.devnull],
            cwd=cwd, env=env, input_text=input_text, timeout=timeout)
        return replace(result, argv=tuple(argv), stdout=result.stdout.replace("\r", ""))


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    return text.replace("\r\n", "\n").replace("\r", "\n")


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
