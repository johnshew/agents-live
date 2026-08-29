"""Native Windows host adapter.

Task Scheduler rendering remains behind this adapter. The generic port never
imports Windows APIs.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from ...legacy import artifacts as legacy_artifacts
from .. import artifacts
from ..grammars import parse_schedule, parse_watch
from ..spawn import cli_executable_path
from ..values import (
    Health,
    InstalledTrigger,
    ProcessRef,
    RenderedSubscription,
    Subscription,
)
from .posix import _address
from .processes import LocalChildRunner
from . import task_scheduler as wintasks
from . import windows_watch as winwatch


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
            description=f"Agents Live subscription {rendered.key}",
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
            metadata = artifacts.from_argv(argv)
            if metadata is not None:
                found.append(InstalledTrigger(
                    metadata.id,
                    metadata.scope,
                    "watch" if "watch-loop" in argv else "schedule",
                    artifacts.PREFIX + metadata.id,
                    json.dumps(task, sort_keys=True, default=str),
                    metadata.target,
                ))
                continue
            legacy = None
            for index, token in enumerate(argv[:-1]):
                if token == "--artifact-marker":
                    legacy = legacy_artifacts.decode(argv[index + 1])
                    break
            if legacy is not None:
                found.append(InstalledTrigger(
                    legacy["key"],
                    legacy["scope"],
                    legacy["kind"],
                    legacy_artifacts.PREFIX + legacy["fingerprint"],
                    json.dumps(task, sort_keys=True, default=str),
                    legacy.get("target", ""),
                ))
        return found

    def clear(self) -> int:
        installed = self.list()
        for trigger in installed:
            self.remove(trigger.key)
        return len(installed)


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
        from . import system as hostruntime
        streams = {}
        if stdout is not None:
            streams["stdout"] = stdout
        if stderr is not None:
            streams["stderr"] = stderr
        process = hostruntime.spawn_detached(
            argv,
            cwd=cwd,
            **streams,
        )
        return ProcessRef(
            process.pid, time.time(), Path(argv[0]).name,
            role, key, fingerprint)

    def alive(self, ref: ProcessRef) -> bool:
        if ref.role == "upgrade":
            from . import system as hostruntime
            if not hostruntime.is_alive(ref.pid):
                return False
            started = hostruntime.process_start_time(ref.pid)
            return started is None or abs(started - ref.created_at) < 2.0
        return any(
            item.pid == ref.pid
            and item.role == ref.role
            and item.key == ref.key
            and item.fingerprint == ref.fingerprint
            for item in self.owned(ref.role)
        )

    def adopt(
        self, pid: int, *, role: str, key: str = "",
        fingerprint: str = "", image: str = "",
    ) -> ProcessRef:
        from . import system as hostruntime
        created_at = hostruntime.process_start_time(pid) or time.time()
        return ProcessRef(
            pid, created_at, image, role, key, fingerprint)

    def defer_until_environment_exits(
        self, argv: Sequence[str], environment: Path | str, **kwargs,
    ) -> ProcessRef | None:
        from . import system as hostruntime
        return hostruntime.defer_until_environment_exits(
            argv, environment, supervisor=self, **kwargs)

    def terminate(self, ref: ProcessRef) -> None:
        if not self.alive(ref):
            return
        from . import system as hostruntime
        hostruntime.terminate(ref.pid)

    def owned(self, role: str | None = None) -> list[ProcessRef]:
        from . import system as hostruntime

        found: list[ProcessRef] = []
        for pid, command_line in hostruntime.process_command_lines():
            try:
                argv = wintasks.parse_command_line(command_line)
            except wintasks.ArgumentQuotingError:
                continue
            markers = _process_markers(argv)
            if markers is None or (role is not None and markers["role"] != role):
                continue
            found.append(ProcessRef(
                pid,
                hostruntime.process_start_time(pid) or 0.0,
                Path(argv[0]).name,
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
        if subscription.target == "runtime":
            root = str(Path.home())
            target = ""
        else:
            root, target = _address(subscription)
        fingerprint = artifacts.PREFIX + subscription.key
        origin = None
        if subscription.kind == "schedule" and subscription.target != "runtime":
            origin = "boot" if parse_schedule(
                subscription.trigger).canonical == "@reboot" else "clock"
        marker = artifacts.encode(artifacts.InvocationMetadata(
            subscription.key,
            subscription.scope,
            subscription.target,
            origin,
        ))
        executable = str(cli_executable_path())
        if subscription.kind == "schedule":
            schedule = parse_schedule(subscription.trigger).canonical
            if subscription.target == "runtime":
                argv = [
                    executable, "internal", "maintain",
                    "--metadata", marker, "--quiet",
                ]
            else:
                argv = [
                    executable, "--repo", root, "run",
                    "--metadata", marker, "--name", target, "--quiet",
                ]
            watcher_argv: tuple[str, ...] = ()
        else:
            watch = parse_watch(subscription.trigger)
            schedule = "@reboot"
            watcher_argv = (
                executable, "--repo", root, "internal", "watch-loop",
                "--metadata", marker, target,
                "--watch-expression", watch.canonical,
            )
            argv = list(watcher_argv[:-2])
        rendered = json.dumps(
            {"argv": argv, "root": root, "schedule": schedule},
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
            subscription.target,
        )

    def legacy_agents(self, root: str) -> set[str]:
        return set(wintasks.installed_names(root))

    def remove_legacy(self, root: str, name: str) -> None:
        wintasks.remove(root, name)

    def change_source(self, roots: Sequence[str]):
        return winwatch.WindowsEventSource(roots)

    def health(self) -> Health:
        from . import task_scheduler as wintasks
        problem = wintasks.probe()
        return Health(problem is None, detail=() if problem is None else (problem,))


def _process_markers(argv: Sequence[str]) -> dict[str, str] | None:
    metadata = artifacts.from_argv(argv)
    if metadata is None or "watch-loop" not in argv:
        return None
    return {
        "role": "watcher",
        "key": metadata.id,
        "fingerprint": artifacts.PREFIX + metadata.id,
    }
