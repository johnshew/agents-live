"""Distro-level Windows heartbeat execution and Task Scheduler lifecycle."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import paths, preflight

TASK_PREFIX = "Agents Live Heartbeat"
LEGACY_TASK = "WSL Heartbeat"
LEGACY_ACTION_TOKENS = (
    "windows-heartbeat.sh", "site-packages", "python3.", "--repo")
INVALID_DISTRO_CHARS = ('"', "\n", "\r", "\0")
LAUNCHER = "wslg.exe"
# Where WSL puts it: the MSI package first, then the Store package's
# execution alias. It is deliberately not on PATH in either.
LAUNCHER_CANDIDATES = (
    r"$env:ProgramFiles\WSL\wslg.exe",
    r"$env:LOCALAPPDATA\Microsoft\WindowsApps\wslg.exe",
)
_launcher: str | None = None


def state_dir() -> Path:
    return paths.state_home()


def beacon_path() -> Path:
    return state_dir() / "heartbeat.ok"


def task_name(distro: str) -> str:
    return f"{TASK_PREFIX} ({distro})"


def current_distro(distro: str | None = None) -> str:
    selected = (distro or os.environ.get("WSL_DISTRO_NAME", "")).strip()
    if not selected:
        raise RuntimeError(
            "cannot determine the WSL distro; pass --distro <name>")
    if any(character in selected for character in INVALID_DISTRO_CHARS):
        raise RuntimeError("invalid WSL distro name")
    return selected


def stable_cli_path() -> Path:
    return Path.home() / ".local" / "bin" / "agents-live"


def windowless_launcher() -> str:
    r"""Where this host keeps ``wslg.exe``, WSL's own windowless launcher.

    A task action runs with an interactive token, in the developer's own
    session, so a console program named directly opens a console window -
    every five minutes, on top of whatever they were doing. ``wslg.exe``
    is the GUI-subsystem build of ``wsl.exe`` that WSL ships to start
    Linux GUI programs: the operating system gives it no console, so
    there is no window to hide and nothing for the default terminal
    application to reopen somewhere visible.

    It ships beside ``wsl.exe`` but is not on PATH, so it is looked up
    where WSL installs it. The answer is cached because it is a property
    of the host, and every caller in a process wants the same one.
    """
    global _launcher
    if _launcher is not None:
        return _launcher
    candidates = ",".join(f'"{candidate}"' for candidate in LAUNCHER_CANDIDATES)
    script = (
        f"$found=$null;foreach ($p in @({candidates})) "
        "{if (Test-Path -LiteralPath $p) {$found=$p;break}};"
        f"if (-not $found) {{$c=Get-Command {LAUNCHER} "
        "-ErrorAction SilentlyContinue;if ($c) {$found=$c.Source}};"
        "if ($found) {$found}")
    resolved = _run_powershell(script).stdout.strip()
    if not resolved:
        raise RuntimeError(
            f"cannot find {LAUNCHER}, which runs the heartbeat without a "
            "console window; it ships with WSL 2, so run `wsl.exe --update` "
            "on the Windows side and try again")
    _launcher = resolved
    return _launcher


def task_action(distro: str, cli_path: Path | None = None,
                launcher: str | None = None) -> tuple[str, str]:
    """(Execute, Arguments) for the scheduled task.

    The two halves of the argument string are quoted by different rules,
    because two different parsers read them: ``wslg.exe`` takes its own
    options from the Windows command line, and hands everything after
    ``--`` to the distro's shell exactly as written.
    """
    return (launcher or windowless_launcher()), " ".join([
        subprocess.list2cmdline(["-d", current_distro(distro)]), "--",
        shlex.join([str(cli_path or stable_cli_path()), "heartbeat"]),
    ])


def run_once() -> int:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["systemctl", "--user", "status"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        pass
    now = datetime.now().astimezone()
    beacon_path().write_text(
        f"alive {now.strftime('%Y-%m-%d %H:%M %Z')}\n", encoding="utf-8")
    with (directory / "heartbeat.log").open("a", encoding="utf-8") as stream:
        stream.write(f"{now.isoformat()} heartbeat: WSL alive\n")
    return 0


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell() -> str:
    found = shutil.which("powershell.exe")
    if found:
        return found
    candidate = Path(
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError("Windows PowerShell interop is unavailable")


def _run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=30)
    if completed.returncode:
        detail = completed.stderr.strip().splitlines()
        raise RuntimeError(
            detail[0][:300] if detail else "Windows Task Scheduler command failed")
    return completed


def _task_exists(name: str) -> bool:
    script = (
        f"$task=Get-ScheduledTask -TaskName {_ps_quote(name)} "
        "-ErrorAction SilentlyContinue;"
        "if ($null -eq $task) { 'false' } else { 'true' }")
    completed = _run_powershell(script)
    answer = completed.stdout.strip().lower()
    if answer not in ("true", "false"):
        raise RuntimeError("Task Scheduler returned unreadable task status")
    return answer == "true"


def _register_task(distro: str, cli_path: Path) -> None:
    name = task_name(distro)
    execute, arguments = task_action(distro, cli_path)
    script = (
        f"$action=New-ScheduledTaskAction -Execute {_ps_quote(execute)} "
        f"-Argument {_ps_quote(arguments)};"
        "$trigger=New-ScheduledTaskTrigger -Once -At (Get-Date) "
        "-RepetitionInterval (New-TimeSpan -Minutes 5);"
        "$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -StartWhenAvailable "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 1);"
        f"Register-ScheduledTask -TaskName {_ps_quote(name)} -Action $action "
        "-Trigger $trigger -Settings $settings "
        "-Description 'Keep this WSL distro available for Agents Live' "
        "-RunLevel Limited -Force | Out-Null")
    _run_powershell(script)


def _start_task(name: str) -> None:
    _run_powershell(
        f"Start-ScheduledTask -TaskName {_ps_quote(name)} -ErrorAction Stop")


def _unregister_task(name: str) -> None:
    _run_powershell(
        f"Unregister-ScheduledTask -TaskName {_ps_quote(name)} "
        "-Confirm:$false -ErrorAction Stop")


def _wait_for_fresh_beacon(previous_mtime: float | None, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = beacon_path().stat().st_mtime
        except OSError:
            current = None
        if current is not None and (
                previous_mtime is None or current > previous_mtime):
            return True
        time.sleep(0.5)
    return False


def install(distro: str | None = None) -> None:
    selected = current_distro(distro)
    env_distro = os.environ.get("WSL_DISTRO_NAME", "").strip()
    if env_distro and selected != env_distro:
        # The install verifies via beacon_path() and stable_cli_path(),
        # both of which live in the *current* distro's filesystem; a
        # cross-distro install would register the task and then always
        # time out waiting for a beacon written somewhere else.
        raise RuntimeError(
            f"--distro {selected!r} does not match the current distro "
            f"{env_distro!r}; run the install inside {selected!r}")
    cli_path = stable_cli_path()
    if not cli_path.is_file() or not os.access(cli_path, os.X_OK):
        raise RuntimeError(
            f"stable uv tool shim not found or executable: {cli_path}; "
            "install with `uv tool install agents-live`")
    try:
        previous_mtime = beacon_path().stat().st_mtime
    except OSError:
        previous_mtime = None
    legacy_exists = _task_exists(LEGACY_TASK)
    _register_task(selected, cli_path)
    _start_task(task_name(selected))
    if not _wait_for_fresh_beacon(previous_mtime):
        raise RuntimeError(
            "the new scheduled task did not write a fresh global heartbeat; "
            f"the legacy {LEGACY_TASK!r} task was left unchanged")
    if legacy_exists:
        _unregister_task(LEGACY_TASK)
    print(f"Installed {task_name(selected)} using {cli_path}")
    if legacy_exists:
        print(f"Migrated and removed legacy task {LEGACY_TASK}")


def uninstall(distro: str | None = None, *, retain_state: bool = False) -> None:
    selected = current_distro(distro)
    name = task_name(selected)
    if _task_exists(name):
        _unregister_task(name)
    if not retain_state:
        for path in (beacon_path(), state_dir() / "heartbeat.log",
                     state_dir() / "crontab.lock"):
            path.unlink(missing_ok=True)
        try:
            state_dir().rmdir()
        except OSError:
            pass
    print(f"Removed {name}")


def task_configuration(distro: str | None = None) -> tuple[dict | None, bool]:
    selected = current_distro(distro)
    name = task_name(selected)
    script = (
        f"$task=Get-ScheduledTask -TaskName {_ps_quote(name)} "
        "-ErrorAction SilentlyContinue;"
        "if ($null -eq $task) { '{}' } else {"
        "[pscustomobject]@{Found=$true;Enabled=$task.Settings.Enabled;"
        "Execute=$task.Actions[0].Execute;Arguments=$task.Actions[0].Arguments;"
        "Interval=$task.Triggers[0].Repetition.Interval} | "
        "ConvertTo-Json -Compress}")
    completed = _run_powershell(script)
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Task Scheduler returned unreadable configuration") from exc
    return (data if data.get("Found") else None), _task_exists(LEGACY_TASK)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run or manage the distro-level Windows heartbeat")
    subparsers = parser.add_subparsers(dest="operation")
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--distro")
    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.add_argument("--distro")
    uninstall_parser.add_argument("--retain-state", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.operation == "install":
            install(args.distro)
        elif args.operation == "uninstall":
            uninstall(args.distro, retain_state=args.retain_state)
        else:
            return run_once()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        preflight.emit_failure("heartbeat", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
