"""Timezone-aware date/time helpers.

Storage convention: timezone-aware ISO-8601 in UTC. Presentation: Asia/Jakarta
(configurable). Parsing never guesses a date that is not present in the input.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from dateutil import parser as du_parser

DEFAULT_TZ = "Asia/Jakarta"


@lru_cache(maxsize=8)
def get_tz(name: str = DEFAULT_TZ) -> ZoneInfo:
    return ZoneInfo(name)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now(tz_name: str = DEFAULT_TZ) -> datetime:
    return datetime.now(get_tz(tz_name))


def to_local(dt: datetime | None, tz_name: str = DEFAULT_TZ) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_tz(tz_name))


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return to_utc(dt).isoformat() if dt else None


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return to_utc(dt)


def format_local(dt: datetime | None, tz_name: str = DEFAULT_TZ, fmt: str = "%Y-%m-%d %H:%M") -> str:
    loc = to_local(dt, tz_name)
    return loc.strftime(fmt) if loc else ""


def folder_timestamp(dt: datetime, tz_name: str = DEFAULT_TZ) -> str:
    return to_local(dt, tz_name).strftime("%Y-%m-%d_%H%M%S")  # type: ignore[union-attr]


_REL_UNITS = {
    "second": 1 / 86400, "sec": 1 / 86400, "detik": 1 / 86400,
    "minute": 1 / 1440, "min": 1 / 1440, "menit": 1 / 1440,
    "hour": 1 / 24, "hr": 1 / 24, "jam": 1 / 24,
    "day": 1, "hari": 1,
    "week": 7, "minggu": 7, "pekan": 7,
    "month": 30, "bulan": 30,
    "year": 365, "tahun": 365,
}
_REL_RE = re.compile(
    r"(?i)\b(\d{1,3}|an?|satu)\s*\+?\s*(seconds?|secs?|detik|minutes?|mins?|menit|hours?|hrs?|jam|days?|hari|weeks?|minggu|pekan|months?|bulan|years?|tahun)\s*(?:ago|yang\s+lalu|lalu)\b"
)
_SHORT_REL_RE = re.compile(r"(?i)^\s*(\d{1,3})\s*([hdwmy])\s*(?:ago)?\s*$")


def parse_relative(text: str, now: datetime | None = None) -> datetime | None:
    """Parse '3 days ago', '2 hari yang lalu', 'yesterday', 'kemarin', '5d'."""
    now = now or utc_now()
    t = text.strip().lower()
    if t in {"today", "hari ini", "just now", "baru saja"}:
        return now
    if t in {"yesterday", "kemarin"}:
        return now - timedelta(days=1)
    m = _REL_RE.search(t)
    if m:
        qty_raw, unit = m.group(1), m.group(2).lower()
        qty = 1 if qty_raw in {"a", "an", "satu"} else int(qty_raw)
        for key, days in _REL_UNITS.items():
            if unit.startswith(key):
                return now - timedelta(days=qty * days)
    m = _SHORT_REL_RE.match(t)
    if m:
        qty, unit = int(m.group(1)), m.group(2).lower()
        days = {"h": 1 / 24, "d": 1, "w": 7, "m": 30, "y": 365}[unit]
        return now - timedelta(days=qty * days)
    return None


def parse_datetime(value: object, *, now: datetime | None = None) -> datetime | None:
    """Parse ISO strings, epoch seconds/milliseconds, relative phrases and common formats.

    Returns an aware UTC datetime, or None when the value cannot be parsed. Values
    in the future by more than a day (clock skew aside) are rejected as implausible.
    """
    now = now or utc_now()
    result: datetime | None = None
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = to_utc(value)
    elif isinstance(value, date):
        result = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{9,13}", value.strip())):
        num = float(value)
        if num > 1e12:  # milliseconds
            num /= 1000.0
        if num <= 0:
            return None
        try:
            result = datetime.fromtimestamp(num, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        rel = parse_relative(s, now)
        if rel is not None:
            result = rel
        else:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                result = to_utc(dt)
            except ValueError:
                try:
                    dt = du_parser.parse(s, fuzzy=False, dayfirst=False)
                    result = to_utc(dt)
                except (ValueError, OverflowError, TypeError):
                    return None
    else:
        return None
    if result is None:
        return None
    if result > now + timedelta(days=1) or result.year < 1995:
        return None
    return result


def age_days(dt: datetime | None, now: datetime | None = None) -> float | None:
    if dt is None:
        return None
    now = now or utc_now()
    return max(0.0, (now - to_utc(dt)).total_seconds() / 86400.0)  # type: ignore[operator]


def within_window(dt: datetime | None, window_days: int | None, now: datetime | None = None) -> bool | None:
    """True/False when the date is known; None when unknown (never guessed)."""
    if window_days is None:
        return True
    if dt is None:
        return None
    a = age_days(dt, now)
    return a is not None and a <= window_days + 0.5
