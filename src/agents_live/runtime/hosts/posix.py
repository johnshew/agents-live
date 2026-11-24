"""POSIX host adapter backed by the user's crontab and process table."""
from __future__ import annotations

import os
import re
import shlex
from collections.abc import Sequence
from pathlib import Path

from ...legacy import artifacts as legacy_artifacts
from .. import artifacts
from ..grammars import parse_schedule, parse_watch
from ..values import Health, InstalledTrigger, RenderedSubscription, Subscription
from .processes import LocalChildRunner, LocalProcesses
from . import crontab as crontasks
from . import filesystem as watchsource


class PosixTriggerStore:
    def install(self, rendered: RenderedSubscription) -> None:
        with crontasks.lock():
            lines = crontasks.lines()
            if lines is None:
                raise RuntimeError("crontab is unreadable")
            conflict = zone_conflict(lines)
            if (conflict and rendered.kind == "schedule" and rendered.target != "runtime"
                    and not rendered.rendered.startswith("@reboot")):
                raise RuntimeError(conflict)
            kept = [line for line in lines if _key(line) != rendered.key]
            crontasks.write([*kept, rendered.rendered])

    def remove(self, key: str) -> None:
        with crontasks.lock():
            lines = crontasks.lines()
            if lines is None:
                raise RuntimeError("crontab is unreadable")
            crontasks.write([line for line in lines if _key(line) != key])

    def list(self) -> list[InstalledTrigger]:
        lines = crontasks.lines()
        if lines is None:
            raise RuntimeError("crontab is unreadable")
        found: list[InstalledTrigger] = []
        for line in lines:
            metadata = artifacts.from_rendered(line)
            if metadata is not None:
                found.append(InstalledTrigger(
                    metadata.id,
                    metadata.scope,
                    _kind(line),
                    artifacts.PREFIX + metadata.id + (
                        ":supervised" if "watch-supervise" in shlex.split(line) else ""),
                    line,
                    metadata.target,
                ))
                continue
            legacy = legacy_artifacts.from_rendered(line)
            if legacy is not None:
                found.append(InstalledTrigger(
                    legacy["key"],
                    legacy["scope"],
                    legacy["kind"],
                    legacy_artifacts.PREFIX + legacy["fingerprint"],
                    line,
                    legacy.get("target", ""),
                ))
        return found

    def clear(self) -> int:
        with crontasks.lock():
            lines = crontasks.lines()
            if lines is None:
                raise RuntimeError("crontab is unreadable")
            kept = [
                line for line in lines
                if artifacts.from_rendered(line) is None
                and legacy_artifacts.from_rendered(line) is None
            ]
            crontasks.write(kept)
            return len(lines) - len(kept)


class PosixHost:
    def __init__(self) -> None:
        self.trigger_store = PosixTriggerStore()
        self.supervisor = LocalProcesses()
        self.child_runner = LocalChildRunner()

    def prepare(self) -> None:
        pass

    def render(self, subscription: Subscription) -> RenderedSubscription:
        if subscription.target == "runtime":
            root = ""
            target = ""
        else:
            root, target = _address(subscription)
        fingerprint = artifacts.PREFIX + subscription.key
        if subscription.kind == "watch":
            fingerprint += ":supervised"
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
        if subscription.kind == "schedule":
            trigger = parse_schedule(subscription.trigger).canonical
            if subscription.target == "runtime":
                argv = [
                    "agents-live", "internal", "maintain",
                    "--metadata", marker, "--quiet",
                ]
            else:
                argv = [
                    "agents-live", "--repo", root, "run",
                    "--metadata", marker, "--name", target, "--quiet",
                ]
            watcher_argv: tuple[str, ...] = ()
        else:
            watch = parse_watch(subscription.trigger)
            trigger = "@reboot"
            watcher_base = (
                "agents-live", "--repo", root, "internal", "watch-loop",
                "--metadata", marker, target,
            )
            watcher_argv = (*watcher_base, "--watch-expression", watch.canonical)
            argv = list(watcher_base)
            argv[4] = "watch-supervise"
        if subscription.target == "runtime":
            rendered = f"{trigger} {shlex.join(argv)} 2>&1"
        else:
            rendered = (
                f"{trigger} cd {shlex.quote(root)} && {shlex.join(argv)} "
                "2>&1"
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
        return set(crontasks.installed_names(root, kind=crontasks.CLOCK)) | set(
            crontasks.installed_names(root, kind=crontasks.WATCH))

    def remove_legacy(self, root: str, name: str) -> None:
        crontasks.remove(root, name)
        crontasks.delete(root, name, kind=crontasks.WATCH)

    def health(self) -> Health:
        lines = crontasks.lines()
        if lines is None:
            return Health(False, detail=("crontab is unreadable",))
        conflict = zone_conflict(lines)
        return Health(conflict is None, detail=(conflict,) if conflict else ())

    def change_source(self, roots: Sequence[str]):
        return watchsource.PosixEventSource([Path(item) for item in roots], cwd=Path.cwd())


# Cron reads the lines after a CRON_TZ assignment in that zone and passes TZ
# to the commands it starts, while the due check reads the machine's local
# clock, so either assignment would make every firing look not due. Cron
# also accepts the name in single or double quotes.
_ZONE_ASSIGNMENT = re.compile(r"""\s*(["']?)(CRON_TZ|TZ)\1\s*=""")


def zone_conflict(lines: Sequence[str]) -> str | None:
    """Why clock lines cannot be added to this crontab, or None."""
    names = sorted({match.group(2) for line in lines
                    if (match := _ZONE_ASSIGNMENT.match(line))})
    if not names:
        return None
    return (f"the crontab sets {' and '.join(names)}, so cron would not run "
            "agent schedules on the machine's local clock; remove that line "
            "with 'crontab -e' or set the machine's time zone instead")


def _address(subscription: Subscription) -> tuple[str, str]:
    if not subscription.scope.startswith("repo:"):
        raise ValueError(f"POSIX agent subscription has invalid scope: {subscription.scope}")
    if not subscription.target.startswith("agent:"):
        raise ValueError(f"POSIX subscription has invalid target: {subscription.target}")
    return subscription.scope.removeprefix("repo:"), subscription.target.removeprefix("agent:")


def _key(line: str) -> str | None:
    metadata = artifacts.from_rendered(line)
    if metadata is not None:
        return metadata.id
    legacy = legacy_artifacts.from_rendered(line)
    return legacy["key"] if legacy else None


def _kind(line: str) -> str:
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    return "watch" if {"watch-loop", "watch-supervise"}.intersection(tokens) else "schedule"
