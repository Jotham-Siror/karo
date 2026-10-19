"""Messages waiting for a member. Each channel (web page, WhatsApp) collects its own rows and marks them delivered."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime

from karo import db, store
from karo.clock import iso

WEB = "web"


def notify(conn: sqlite3.Connection, member_id: str, text: str, now: datetime) -> list[int]:
    """One row per channel the member has an identity on, and always one for the web page."""
    with db.transaction(conn):
        channels = {WEB} | {channel for channel, _ in store.identities_of(conn, member_id)}
        return [
            conn.execute(
                "INSERT INTO outbox (member_id, channel, text, created_at) VALUES (?, ?, ?, ?)",
                (member_id, channel, text, iso(now)),
            ).lastrowid
            for channel in sorted(channels)
        ]


def pending(conn: sqlite3.Connection, member_id: str, channel: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM outbox WHERE member_id = ? AND channel = ? AND delivered_at IS NULL ORDER BY id",
        (member_id, channel),
    ).fetchall()


def mark_delivered(conn: sqlite3.Connection, ids: Iterable[int], now: datetime) -> None:
    # Only undelivered rows: a second pass must not overwrite when the message really went out.
    conn.executemany(
        "UPDATE outbox SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL", [(iso(now), i) for i in ids]
    )
