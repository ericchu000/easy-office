"""Small, dependency-free helpers shared across the foundation modules."""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Treat naive datetimes as UTC and convert aware ones to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string (seconds precision)."""
    return ensure_utc(value).isoformat(timespec="seconds")


def coerce_datetime(
    value: str | datetime | date | int | float | None,
    *,
    default: datetime | None = None,
    field: str = "timestamp",
) -> datetime:
    """Coerce common timestamp representations into an aware UTC datetime."""
    if value is None:
        if default is None:
            raise ValueError(f"{field} is required")
        return ensure_utc(default)
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            if default is None:
                raise ValueError(f"{field} must not be empty")
            return ensure_utc(default)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return ensure_utc(datetime.fromisoformat(text))
        except ValueError:
            pass
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except ValueError as exc:
            raise ValueError(f"{field} is not a valid ISO timestamp: {value!r}") from exc
    raise ValueError(f"Unsupported {field} type: {type(value).__name__}")


def coerce_iso_date(value: str | date | datetime | None, *, field: str = "date") -> str | None:
    """Normalize a business date into ``YYYY-MM-DD``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            pass
        try:
            normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
            return ensure_utc(datetime.fromisoformat(normalized)).date().isoformat()
        except ValueError as exc:
            raise ValueError(f"{field} is not a valid ISO date: {value!r}") from exc
    raise ValueError(f"Unsupported {field} type: {type(value).__name__}")


def json_dumps(value: Any) -> str:
    """Stable JSON serialization used for database JSON columns."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | bytes | None, default: Any = None) -> Any:
    """Return ``default`` for NULL or malformed JSON instead of raising."""
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write text atomically: temp file in the same directory, then os.replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with open(temp, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def new_uid() -> str:
    """Return a URL/path-safe unique identifier."""
    return uuid.uuid4().hex
