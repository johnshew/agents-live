"""The fixed UTC offset a clock trigger keeps from registration (#609).

Registration is the only place local time is read. The offset in force
then is written onto the wake entry, and every later decision reads the
schedule at UTC plus that offset, so a daylight saving change, a time
zone switch or a scheduler that assumes a different zone cannot move a
firing. Stopping and starting an agent registers it again from the
current local time.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

FLAG = "--utc-offset"
_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})")
# Finds the flag in a crontab line, a command line or a JSON rendering.
_IN_TEXT = re.compile(r"--utc-offset[\s\"',\\]+([+-]\d{2}:\d{2})(?!\d)")


def format_offset(delta: timedelta) -> str:
    """``+HH:MM`` for a whole-minute *delta*."""
    minutes = int(delta.total_seconds()) // 60
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"


def current_offset(now: datetime | None = None) -> str:
    """The local UTC offset at *now*, as ``+HH:MM``."""
    moment = (now or datetime.now(UTC)).astimezone()
    return format_offset(moment.utcoffset() or timedelta())


def zone(offset: str) -> timezone:
    """The fixed zone ``+HH:MM`` names. Raises ValueError otherwise."""
    match = _OFFSET.fullmatch(offset.strip())
    if match is None:
        raise ValueError(f"{FLAG} must be +HH:MM or -HH:MM, not {offset!r}")
    sign, hours, minutes = match.groups()
    if int(hours) > 23 or int(minutes) > 59:
        raise ValueError(f"{FLAG} is out of range: {offset!r}")
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    return timezone(-delta if sign == "-" else delta)


def offset_in(text: str) -> str | None:
    """The offset a rendered wake entry carries, or None."""
    match = _IN_TEXT.search(text)
    return match.group(1) if match else None


def short_zone(moment: datetime) -> str:
    """A short name for *moment*'s zone, for display only.

    Windows reports long names (``Arabian Standard Time``), which are
    shortened to their initials; a name already short is kept.
    """
    name = moment.tzname() or ""
    if name.casefold() in {"coordinated universal time", "universal coordinated time"}:
        return "UTC"
    if " " in name:
        return "".join(word[0] for word in re.findall(r"[A-Za-z]+", name)).upper()
    return name or format_offset(moment.utcoffset() or timedelta())


def display(moment: datetime) -> str:
    """*moment* in the local zone as ``YYYY-MM-DD HH:MM ZONE``."""
    local = moment.astimezone()
    return f"{local:%Y-%m-%d %H:%M} {short_zone(local)}"
