"""A tamper-evident audit log: each row's hash covers the previous row's hash.

The database triggers stop the app from rewriting history. The chain catches someone who edits the
file directly and drops the triggers first.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any

from karo import db
from karo.clock import iso

GENESIS = "0" * 64

_SECRET_WORDS = frozenset({"pin", "token", "secret", "password"})


def _looks_secret(key: object) -> bool:
    lowered = str(key).lower()
    return lowered == "api_key" or any(part in _SECRET_WORDS for part in lowered.split("_"))


def _refuse_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            if _looks_secret(key):
                raise ValueError(f"audit data must not carry secrets (key {key!r})")
            _refuse_secrets(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            _refuse_secrets(inner)


def _hash(prev_hash: str, at: str, family_id: str | None, actor: str, event: str, data_json: str) -> str:
    # Record and verify share this, and both go through the stored text, so they cannot disagree about how data was encoded.
    canonical = json.dumps(
        {"at": at, "family_id": family_id, "actor": actor, "event": event, "data": json.loads(data_json)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


def record(conn: sqlite3.Connection, *, at: datetime, family_id: str | None, actor: str, event: str, data: dict) -> int:
    _refuse_secrets(data)
    stamp = iso(at)
    data_json = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    with db.transaction(conn):
        head = conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = head["hash"] if head else GENESIS
        digest = _hash(prev_hash, stamp, family_id, actor, event, data_json)
        cursor = conn.execute(
            "INSERT INTO audit (at, family_id, actor, event, data_json, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (stamp, family_id, actor, event, data_json, prev_hash, digest),
        )
    return cursor.lastrowid


def verify(conn: sqlite3.Connection) -> tuple[bool, int | None]:
    prev_hash = GENESIS
    for row in conn.execute("SELECT * FROM audit ORDER BY seq"):
        try:
            expected = _hash(prev_hash, row["at"], row["family_id"], row["actor"], row["event"], row["data_json"])
        except (ValueError, RecursionError):
            # Verifying is the one place that reads stored text nobody vouches for: JSON that does not parse
            # (JSONDecodeError and UnicodeDecodeError are both ValueErrors) or nests absurdly deep is a bad row, not a crash.
            return False, row["seq"]
        if row["prev_hash"] != prev_hash or row["hash"] != expected:
            return False, row["seq"]
        prev_hash = row["hash"]
    return True, None


def events(conn: sqlite3.Connection, family_id: str | None = None, limit: int = 200) -> list[dict]:
    """Newest first. With a family id, only that family's rows (never system-wide ones)."""
    where, args = ("WHERE family_id = ?", (family_id, limit)) if family_id is not None else ("", (limit,))
    rows = conn.execute(f"SELECT * FROM audit {where} ORDER BY seq DESC LIMIT ?", args).fetchall()
    return [
        {
            "seq": r["seq"],
            "at": r["at"],
            "family_id": r["family_id"],
            "actor": r["actor"],
            "event": r["event"],
            "data": json.loads(r["data_json"]),
            "hash": r["hash"],
        }
        for r in rows
    ]
