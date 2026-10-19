"""The one place that knows the time. The demo clock is a stored offset, so a fast-forward survives restarts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from karo import db

_OFFSET_KEY = "demo_offset_seconds"


def offset(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (_OFFSET_KEY,)).fetchone()
    return int(row["value"]) if row else 0


def now(conn: sqlite3.Connection) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=offset(conn))


def _set_offset(conn: sqlite3.Connection, seconds: int) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_OFFSET_KEY, str(seconds)),
    )


def advance(conn: sqlite3.Connection, seconds: int) -> None:
    with db.transaction(conn):
        _set_offset(conn, offset(conn) + seconds)


def reset(conn: sqlite3.Connection) -> None:
    _set_offset(conn, 0)


def iso(dt: datetime) -> str:
    if dt.utcoffset() is None:
        raise ValueError("naive datetime: every time in Karo must carry a timezone")
    return dt.astimezone(UTC).isoformat()


def parse(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.utcoffset() is None:
        raise ValueError(f"timestamp {s!r} has no timezone")
    return dt.astimezone(UTC)
