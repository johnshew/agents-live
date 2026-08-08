"""Canonical schedule and watch grammars shared by every host."""
from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

_SPECIALS = {
    "@annually": "@yearly",
    "@yearly": "@yearly",
    "@monthly": "@monthly",
    "@weekly": "@weekly",
    "@daily": "@daily",
    "@midnight": "@daily",
    "@hourly": "@hourly",
    "@reboot": "@reboot",
}
_MONTHS = {name: str(index) for index, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_WEEKDAYS = {name: str(index) for index, name in enumerate(
    ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))}
_FIELD = re.compile(r"^(?:\*|[A-Za-z0-9]+)(?:-[A-Za-z0-9]+)?(?:/[0-9]+)?$")


class ScheduleSyntaxError(ValueError):
    pass


class WatchSyntaxError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    canonical: str

    def matches(self, moment: datetime) -> bool:
        expression = {
            "@yearly": "0 0 1 1 *",
            "@monthly": "0 0 1 * *",
            "@weekly": "0 0 * * 0",
            "@daily": "0 0 * * *",
            "@hourly": "0 * * * *",
        }.get(self.canonical, self.canonical)
        if expression == "@reboot":
            return False
        fields = expression.split()
        limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
        values = [
            _expand_field(field, low, high)
            for field, (low, high) in zip(fields, limits, strict=True)
        ]
        if moment.minute not in values[0] or moment.hour not in values[1]:
            return False
        if moment.month not in values[3]:
            return False
        day_matches = moment.day in values[2]
        weekday_matches = ((moment.weekday() + 1) % 7) in {
            0 if item == 7 else item for item in values[4]}
        if fields[2] != "*" and fields[4] != "*":
            return day_matches or weekday_matches
        return day_matches and weekday_matches


@dataclass(frozen=True)
class Watch:
    includes: tuple[str, ...]
    excludes: tuple[str, ...]
    debounce_ms: int
    canonical: str

    def matches(self, path: str) -> bool:
        normalized = PurePosixPath(path.replace("\\", "/")).as_posix().lstrip("./")
        return (
            any(fnmatch.fnmatchcase(normalized, pattern) for pattern in self.includes)
            and not any(fnmatch.fnmatchcase(normalized, pattern) for pattern in self.excludes)
        )


def _canonical_value(value: str, names: dict[str, str], low: int, high: int) -> str:
    upper = value.upper()
    if upper in names:
        return names[upper]
    if not value.isdigit():
        raise ScheduleSyntaxError(f"unknown field value: {value}")
    number = int(value)
    if not low <= number <= high:
        raise ScheduleSyntaxError(f"field value {number} is outside {low}-{high}")
    return str(number)


def _canonical_item(item: str, names: dict[str, str], low: int, high: int) -> str:
    if not _FIELD.fullmatch(item):
        raise ScheduleSyntaxError(f"invalid schedule item: {item}")
    base, slash, step = item.partition("/")
    if slash and (not step.isdigit() or int(step) <= 0):
        raise ScheduleSyntaxError(f"invalid schedule step: {item}")
    if base == "*":
        rendered = base
    elif "-" in base:
        first, last = base.split("-", 1)
        first_value = _canonical_value(first, names, low, high)
        last_value = _canonical_value(last, names, low, high)
        if high == 7 and last.upper() == "SUN" and int(first_value) > 0:
            last_value = "7"
        if int(first_value) > int(last_value):
            raise ScheduleSyntaxError(f"schedule range runs backwards: {item}")
        rendered = (
            f"{first_value}-{last_value}"
        )
    else:
        rendered = _canonical_value(base, names, low, high)
    return f"{rendered}/{int(step)}" if slash else rendered


def _canonical_field(field: str, names: dict[str, str], low: int, high: int) -> str:
    if not field:
        raise ScheduleSyntaxError("empty schedule field")
    items = [_canonical_item(item, names, low, high) for item in field.split(",")]
    if high == 7:
        items = ["0" if item == "7" else item for item in items]
    return ",".join(items)


def _expand_field(field: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        base, slash, step_text = part.partition("/")
        step = int(step_text) if slash else 1
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, last = base.split("-", 1)
            start, end = int(first), int(last)
        else:
            start = int(base)
            end = high if slash else start
        values.update(range(start, end + 1, step))
    return values


def parse_schedule(expression: str) -> Schedule:
    source = expression.strip()
    if source.startswith("@"):
        try:
            return Schedule(_SPECIALS[source.lower()])
        except KeyError:
            raise ScheduleSyntaxError(f"unknown schedule keyword: {source}") from None
    fields = source.split()
    if len(fields) != 5:
        raise ScheduleSyntaxError("schedule must contain five fields")
    limits = (
        ({}, 0, 59),
        ({}, 0, 23),
        ({}, 1, 31),
        (_MONTHS, 1, 12),
        (_WEEKDAYS, 0, 7),
    )
    canonical = [
        _canonical_field(field, names, low, high)
        for field, (names, low, high) in zip(fields, limits, strict=True)
    ]
    return Schedule(" ".join(canonical))


def _duration_ms(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)(ms|s|m)", value)
    if not match:
        raise WatchSyntaxError(f"invalid debounce duration: {value}")
    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        raise WatchSyntaxError("debounce duration must be positive")
    return amount * {"ms": 1, "s": 1000, "m": 60_000}[unit]


def _render_duration(milliseconds: int) -> str:
    if milliseconds % 60_000 == 0:
        return f"{milliseconds // 60_000}m"
    if milliseconds % 1000 == 0:
        return f"{milliseconds // 1000}s"
    return f"{milliseconds}ms"


def parse_watch(expression: str, *, default_debounce_ms: int = 1000) -> Watch:
    try:
        tokens = shlex.split(expression, posix=True)
    except ValueError as exc:
        raise WatchSyntaxError(str(exc)) from exc
    if not tokens:
        raise WatchSyntaxError("watch expression is empty")
    debounce_ms = default_debounce_ms
    if "debounce" in tokens:
        positions = [index for index, token in enumerate(tokens) if token == "debounce"]
        if len(positions) != 1 or positions[0] != len(tokens) - 2:
            raise WatchSyntaxError("debounce must occur once at the end")
        debounce_ms = _duration_ms(tokens[-1])
        tokens = tokens[:-2]
    includes = sorted({
        _watch_pattern(token) for token in tokens if not token.startswith("!")})
    excludes = sorted({
        _watch_pattern(token[1:]) for token in tokens if token.startswith("!")})
    if not includes or any(not item for item in (*includes, *excludes)):
        raise WatchSyntaxError("watch expression requires at least one include")
    canonical_tokens = [*includes, *(f"!{item}" for item in excludes)]
    canonical_tokens.extend(("debounce", _render_duration(debounce_ms)))
    return Watch(tuple(includes), tuple(excludes), debounce_ms, shlex.join(canonical_tokens))


def _watch_pattern(value: str) -> str:
    normalized = value.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized)
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise WatchSyntaxError(
            f"watch patterns must be repository-relative: {value}")
    return normalized
