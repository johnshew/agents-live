"""Native Windows host adapter.

Task Scheduler rendering remains behind this adapter. The generic port never
imports Windows APIs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from ... import wintasks, winwatch
from .. import artifacts
from ..grammars import parse_schedule, parse_watch
from ..values import (
    Health,
    InstalledTrigger,
    ProcessRef,
    RenderedSubscription,
    Subscription,
)
from .posix import _address
from .processes import LocalChildRunner


class WindowsTriggerStore:
    _PREFIX = "Subscription-"

    def install(self, rendered: RenderedSubscription) -> None:
        data = json.loads(rendered.rendered)
        path = f"{wintasks.TASK_FOLDER}\\{self._PREFIX}{rendered.key}"
        command, arguments = wintasks.action_form(data["argv"][0], data["argv"][1:])
        document = wintasks.build_task_xml(
            command=command,
            arguments=arguments,
            working_dir=data["root"],
            schedules=[data["schedule"]],
            description=f"Agents Live subscription {data['marker']}",
            uri=path,
            user_id=wintasks.current_user_id(),
        )
        handle, xml_file = tempfile.mkstemp(suffix=".xml", prefix="agents-live-task-")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(document.encode("utf-16"))
            code, _out, error = wintasks._run(
                ["/Create", "/TN", path, "/XML", xml_file, "/F"])
        finally:
            Path(xml_file).unlink(missing_ok=True)
        if code != 0:
            raise wintasks.TaskError(
                error.strip() or f"Task Scheduler refused to register {path}")

    def remove(self, key: str) -> None:
        path = f"{wintasks.TASK_FOLDER}\\{self._PREFIX}{key}"
        code, _out, error = wintasks._run(["/Delete", "/TN", path, "/F"])
        if code not in (0, 1):
            raise wintasks.TaskError(error.strip() or f"could not remove {path}")

    def list(self) -> list[InstalledTrigger]:
        found: list[InstalledTrigger] = []
        for task in wintasks.registered_tasks():
            if not task["name"].startswith(self._PREFIX):
                continue
            try:
                argv = wintasks.parse_command_line(task["arguments"])
            except wintasks.ArgumentQuotingError:
                continue
            marker = None
            for index, token in enumerate(argv[:-1]):
                if token == "--artifact-marker":
                    marker = artifacts.decode(argv[index + 1])
                    break
            if marker is None:
                continue
            found.append(InstalledTrigger(
                marker["key"],
                marker["scope"],
                marker["kind"],
                marker["fingerprint"],
                json.dumps(task, sort_keys=True, default=str),
            ))
        return found


class WindowsProcesses:
    def spawn_detached(
        self,
        argv: Sequence[str],
        *,
        role: str,
        key: str = "",
        fingerprint: str = "",
        cwd: str | None = None,
        stdout=None,
        stderr=None,
    ) -> ProcessRef:
        marked = [
            *argv,
            "--runtime-role", role,
            "--subscription-key", key,
            "--subscription-fingerprint", fingerprint,
        ]
        process = subprocess.Popen(
            marked,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.DEVNULL,
            stderr=stderr if stderr is not None else subprocess.DEVNULL,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
            ),
        )
        return ProcessRef(
            process.pid, time.time(), Path(argv[0]).name,
            role, key, fingerprint)

    def alive(self, ref: ProcessRef) -> bool:
        return any(
            item.pid == ref.pid
            and item.role == ref.role
            and item.key == ref.key
            and item.fingerprint == ref.fingerprint
            for item in self.owned(ref.role)
        )

    def terminate(self, ref: ProcessRef) -> None:
        if not self.alive(ref):
            return
        subprocess.run(
            ["taskkill.exe", "/PID", str(ref.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )

    def owned(self, role: str | None = None) -> list[ProcessRef]:
        command = (
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,CreationDate,ExecutablePath,CommandLine | "
            "ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return []
        try:
            document = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return []
        rows = document if isinstance(document, list) else [document]
        found = []
        for row in rows:
            command_line = row.get("CommandLine") if isinstance(row, dict) else None
            if not command_line:
                continue
            try:
                argv = wintasks.parse_command_line(command_line)
            except wintasks.ArgumentQuotingError:
                continue
            markers = _process_markers(argv)
            if markers is None or (role is not None and markers["role"] != role):
                continue
            found.append(ProcessRef(
                int(row["ProcessId"]),
                _windows_timestamp(row.get("CreationDate")),
                Path(row.get("ExecutablePath") or argv[0]).name,
                markers["role"],
                markers["key"],
                markers["fingerprint"],
            ))
        return found


class WindowsHost:
    def __init__(self) -> None:
        self.trigger_store = WindowsTriggerStore()
        self.supervisor = WindowsProcesses()
        self.child_runner = LocalChildRunner()

    def prepare(self) -> None:
        pass

    def render(self, subscription: Subscription) -> RenderedSubscription:
        import hashlib

        if subscription.target == "runtime":
            root = str(Path.home())
            target = ""
        else:
            root, target = _address(subscription)
        fingerprint = hashlib.sha256(
            f"{subscription.scope}\0{subscription.target}\0{subscription.kind}\0"
            f"{subscription.trigger}".encode()).hexdigest()
        marker = artifacts.encode({
            "fingerprint": fingerprint,
            "key": subscription.key,
            "kind": subscription.kind,
            "scope": subscription.scope,
        })
        executable = shutil.which("agents-live")
        if executable is None:
            raise RuntimeError("agents-live executable is not available")
        if subscription.kind == "schedule":
            schedule = parse_schedule(subscription.trigger).canonical
            if subscription.target == "runtime":
                argv = [executable, "internal", "maintain", "--quiet"]
            else:
                argv = [executable, "--repo", root, "run", "--name", target]
                argv.extend(("--boot", "--quiet") if schedule == "@reboot" else ("--scheduled", "--quiet"))
            watcher_argv: tuple[str, ...] = ()
        else:
            watch = parse_watch(subscription.trigger)
            schedule = "@reboot"
            watcher_argv = (
                executable, "--repo", root, "internal", "watch-loop", target,
                "--watch-expression", watch.canonical,
            )
            argv = [
                *watcher_argv,
                "--runtime-role", "watcher",
                "--subscription-key", subscription.key,
                "--subscription-fingerprint", fingerprint,
            ]
        argv.extend(("--artifact-marker", marker))
        rendered = json.dumps(
            {"argv": argv, "marker": marker, "root": root, "schedule": schedule},
            sort_keys=True,
            separators=(",", ":"),
        )
        return RenderedSubscription(
            subscription.key,
            subscription.scope,
            subscription.kind,
            fingerprint,
            rendered,
            watcher_argv,
        )

    def change_source(self, roots: Sequence[str]):
        return winwatch.WindowsEventSource(roots)

    def health(self) -> Health:
        from ... import wintasks
        problem = wintasks.probe()
        return Health(problem is None, detail=() if problem is None else (problem,))


def _process_markers(argv: Sequence[str]) -> dict[str, str] | None:
    fields = {
        "--runtime-role": "role",
        "--subscription-key": "key",
        "--subscription-fingerprint": "fingerprint",
    }
    values = {}
    for index, token in enumerate(argv[:-1]):
        if token in fields:
            values[fields[token]] = argv[index + 1]
    return values if set(fields.values()) <= values.keys() else None


def _windows_timestamp(value: object) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
