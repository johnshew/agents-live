"""Clock expectations recorded by convergence and audited by read-only queries."""
from __future__ import annotations

import json
import hashlib
import os
import time
from heapq import merge
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .. import paths
from ..runtime.grammars import parse_schedule
from .events import create, record
from .timing import current as current_identity

MISFIRE_GRACE_S = 120


def now() -> datetime:
    return datetime.now(timezone.utc)


def timezone_signature() -> str:
    material = json.dumps([os.environ.get("TZ"), time.tzname, time.timezone,
                           time.altzone, time.daylight], sort_keys=True)
    return "host-local:" + hashlib.sha256(material.encode()).hexdigest()[:24]


def observe(subscriptions, *, protected_scopes=(), protected_targets=(),
            complete: bool = True) -> None:
    """Caller holds the launch gate and has committed complete desired intent."""
    location = paths.state_home() / "clock-expectations.json"
    try:
        previous = json.loads(location.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    desired: dict[str, dict[str, list[str]]] = {}
    for item in subscriptions:
        if (item.kind != "schedule" or not item.scope.startswith("repo:")
                or item.trigger == "@reboot"):
            continue
        repository = item.scope.removeprefix("repo:")
        identifier = item.target.removeprefix("agent:")
        desired.setdefault(repository, {}).setdefault(identifier, []).append(item.trigger)
    for repository, agents in previous.items():
        if not isinstance(agents, dict):
            continue
        for identifier, schedules in agents.items():
            if (f"repo:{repository}" in protected_scopes
                    or f"agent:{identifier}" in protected_targets):
                desired.setdefault(repository, {}).setdefault(identifier, schedules)
    for repository in sorted(previous.keys() | desired.keys()):
        old = previous.get(repository, {})
        if not isinstance(old, dict):
            old = {}
        current = desired.get(repository, {})
        for identifier in sorted(old.keys() | current.keys()):
            schedules = sorted(set(current.get(identifier, [])))
            event = create(
                "clock-schedule", "observed", repository=repository,
                agent=identifier, run_id=current_identity("convergence").run_id, origin="clock",
                attributes=(("schedules", schedules if complete else None),
                            ("schedule_timezone", timezone_signature())),
            )
            record(paths.host_logs_dir() / "clock-schedules.jsonl",
                   replace(event, timestamp=now().isoformat()))
    paths.atomic_write_text(location, json.dumps(desired, sort_keys=True))


def expected(snapshots, *, until: datetime | None = None,
             since: datetime | None = None, before: datetime | None = None,
             local_timezone=None):
    """Yield expected UTC slots only within retained, explicitly observed intent.

    The native scheduler uses this host's local timezone. Candidate enumeration
    in UTC preserves skipped/repeated DST minutes without scanning every minute.
    """
    boundary = (until or now()).astimezone(timezone.utc) - timedelta(seconds=MISFIRE_GRACE_S)
    if before is not None:
        boundary = min(boundary, before)
    grouped = {}
    for timestamp, repository, identifier, schedules, version, generation, zone in snapshots:
        try:
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            expressions = json.loads(schedules) if isinstance(schedules, str) else schedules
            parsed = tuple(parse_schedule(item) for item in expressions)
            if zone != timezone_signature():
                parsed = ()
        except (ValueError, TypeError):
            parsed = ()
        grouped.setdefault((repository, identifier), []).append(
            (timestamp, parsed, version, generation))
    for (repository, identifier), observations in grouped.items():
        observations.sort(key=lambda row: row[0])
        for index, (timestamp, schedules, version, generation) in enumerate(observations):
            end = min(boundary, observations[index + 1][0]) if index + 1 < len(observations) else boundary
            start = max(timestamp, since) if since is not None else timestamp
            previous = None
            for moment in merge(*(schedule.slots(start, end, zone=local_timezone)
                                  for schedule in schedules)):
                if moment != previous:
                    yield (repository, identifier, moment, version, generation)
                    previous = moment
