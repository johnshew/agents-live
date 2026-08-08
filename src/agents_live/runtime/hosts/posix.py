"""POSIX host adapter backed by the user's crontab and process table."""
from __future__ import annotations

import hashlib
import os
import shlex
from collections.abc import Sequence
from pathlib import Path

from ... import crontasks, watchsource
from .. import artifacts
from ..grammars import parse_schedule, parse_watch
from ..values import Health, InstalledTrigger, RenderedSubscription, Subscription
from .processes import LocalChildRunner, LocalProcesses


class PosixTriggerStore:
    def install(self, rendered: RenderedSubscription) -> None:
        with crontasks.lock():
            lines = crontasks.lines()
            if lines is None:
                raise RuntimeError("crontab is unreadable")
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
            marker = artifacts.from_rendered(line)
            if marker is None:
                continue
            found.append(InstalledTrigger(
                marker["key"],
                marker["scope"],
                marker["kind"],
                marker["fingerprint"],
                line,
            ))
        return found


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
        fingerprint = hashlib.sha256(
            f"{subscription.scope}\0{subscription.target}\0{subscription.kind}\0"
            f"{subscription.trigger}".encode()).hexdigest()
        marker = artifacts.encode({
            "fingerprint": fingerprint,
            "key": subscription.key,
            "kind": subscription.kind,
            "scope": subscription.scope,
        })
        if subscription.kind == "schedule":
            trigger = parse_schedule(subscription.trigger).canonical
            if subscription.target == "runtime":
                argv = ["agents-live", "internal", "maintain", "--quiet"]
            else:
                argv = ["agents-live", "--repo", root, "run", "--name", target]
                argv.extend(("--boot", "--quiet") if trigger == "@reboot" else ("--scheduled", "--quiet"))
            watcher_argv: tuple[str, ...] = ()
        else:
            watch = parse_watch(subscription.trigger)
            trigger = "@reboot"
            watcher_argv = (
                "agents-live", "--repo", root, "internal", "watch-loop", target,
                "--watch-expression", watch.canonical,
            )
            argv = [
                *watcher_argv,
                "--runtime-role", "watcher",
                "--subscription-key", subscription.key,
                "--subscription-fingerprint", fingerprint,
                "--artifact-marker", marker,
            ]
        if subscription.target == "runtime":
            rendered = f"{trigger} {shlex.join(argv)} 2>&1 # {marker}"
        else:
            rendered = (
                f"{trigger} cd {shlex.quote(root)} && {shlex.join(argv)} "
                f"2>&1 # {marker}"
            )
        return RenderedSubscription(
            subscription.key,
            subscription.scope,
            subscription.kind,
            fingerprint,
            rendered,
            watcher_argv,
        )

    def health(self) -> Health:
        readable = crontasks.lines() is not None
        return Health(readable, detail=() if readable else ("crontab is unreadable",))

    def change_source(self, roots: Sequence[str]):
        return watchsource.PosixEventSource([Path(item) for item in roots], cwd=Path.cwd())


def _address(subscription: Subscription) -> tuple[str, str]:
    if not subscription.scope.startswith("repo:"):
        raise ValueError(f"POSIX agent subscription has invalid scope: {subscription.scope}")
    if not subscription.target.startswith("agent:"):
        raise ValueError(f"POSIX subscription has invalid target: {subscription.target}")
    return subscription.scope.removeprefix("repo:"), subscription.target.removeprefix("agent:")


def _key(line: str) -> str | None:
    marker = artifacts.from_rendered(line)
    return marker["key"] if marker else None
