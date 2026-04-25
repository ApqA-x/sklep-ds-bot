from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return ensure_utc(datetime.fromisoformat(raw))
    raise TypeError(f"unsupported datetime value {value!r}")


def datetime_to_json(value: datetime | None) -> str | None:
    value = ensure_utc(value)
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def positive_delta(value: timedelta) -> timedelta:
    if value.total_seconds() < 0:
        return timedelta()
    return value


def milliseconds_between(start: datetime, end: datetime) -> int:
    delta = positive_delta(ensure_utc(end) - ensure_utc(start))  # type: ignore[operator]
    return int(delta.total_seconds() * 1000)


def go_duration(value: timedelta | None, *, round_seconds: bool = False) -> str:
    if value is None:
        return "0s"
    value = positive_delta(value)
    total = value.total_seconds()
    if round_seconds:
        total = int(total + 0.5)
    if total < 1 and total != 0:
        millis = int(total * 1000)
        if millis > 0:
            return f"{millis}ms"
        micros = int(total * 1_000_000)
        if micros > 0:
            return f"{micros}us"
    seconds = int(total)
    if seconds == 0:
        return "0s"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes}m{secs}s"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def hms_duration(value: timedelta | None, *, round_seconds: bool = True) -> str:
    if value is None:
        return "0:00:00"
    value = positive_delta(value)
    total = value.total_seconds()
    if round_seconds:
        total = int(total + 0.5)
    else:
        total = int(total)
    if total < 0:
        total = 0
    hours, rem = divmod(int(total), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def discord_timestamp(value: datetime | None, style: str = "F") -> str:
    value = ensure_utc(value)
    if value is None:
        return "unknown"
    unix = int(value.timestamp())
    if style == "relative":
        return f"<t:{unix}:R>"
    return f"<t:{unix}:F> (<t:{unix}:R>)"
