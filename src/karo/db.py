"""SQLite connection, schema set-up, explicit transactions and id generation."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection in autocommit mode: every transaction in Karo is an explicit `transaction()`."""
    in_memory = str(path) == ":memory:"
    if not in_memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # Set before switching to WAL, which itself needs a lock.
    conn.execute("PRAGMA busy_timeout=5000")
    if not in_memory:
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    schema = (resources.files("karo") / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolled back on any exception. Joins an outer transaction if there is one."""
    if conn.in_transaction:
        yield conn
        return
    # IMMEDIATE takes the write lock up front, so two writers queue instead of deadlocking on upgrade.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        # SQLite rolls back by itself on some errors (disk full, interrupt); a second ROLLBACK would hide the real one.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"
