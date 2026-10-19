"""The database layer and the tamper-evident audit log.

Every test names the bug it would catch. Audit tampering is simulated from a second connection,
the way someone with the database file in hand would do it.
"""

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime

import pytest

from karo import audit, db

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "data" / "karo.db"


@pytest.fixture
def conn(db_path):
    connection = db.connect(db_path)
    db.init(connection)
    yield connection
    connection.close()


def record(conn, event="e", *, family_id="fam-1", data=None, at=NOW):
    return audit.record(conn, at=at, family_id=family_id, actor="system", event=event, data=data or {})


def tamper(db_path, sql):
    """Run `sql` on a second connection after dropping the append-only triggers, as someone holding the file would."""
    attacker = db.connect(db_path)
    try:
        attacker.execute("DROP TRIGGER audit_no_update")
        attacker.execute(sql)
    finally:
        attacker.close()


def audit_rows(conn):
    return conn.execute("SELECT * FROM audit ORDER BY seq").fetchall()


# --- db.connect / db.init ----------------------------------------------------------------


def test_connect_creates_the_parent_folder_and_uses_wal(db_path):
    # Bug caught: first start-up crashing because the data folder does not exist yet, or readers blocking the writer.
    connection = db.connect(db_path)
    try:
        assert db_path.parent.is_dir()
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        connection.close()


def test_in_memory_database_works_without_a_folder_or_wal():
    # Bug caught: ":memory:" being treated as a file path (stray folder, or WAL refused).
    connection = db.connect(":memory:")
    db.init(connection)
    assert connection.execute("SELECT count(*) FROM audit").fetchone()[0] == 0
    connection.close()


def test_foreign_keys_are_enforced_on_every_new_connection(db_path, conn):
    # Bug caught: foreign keys are off by default in SQLite and the setting is per connection, so rows could point nowhere.
    second = db.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            second.execute(
                "INSERT INTO members (id, family_id, role, name, country) VALUES ('m1', 'no-such-family', 'relative', 'A', 'KE')"
            )
    finally:
        second.close()


def test_rows_can_be_read_by_column_name(conn):
    # Bug caught: callers indexing by position would break the moment a column is added.
    conn.execute("INSERT INTO settings (key, value) VALUES ('k', 'v')")
    assert conn.execute("SELECT value FROM settings").fetchone()["value"] == "v"


def test_init_twice_keeps_existing_data(conn):
    # Bug caught: a restart re-running the schema and wiping or failing on tables that already exist.
    conn.execute("INSERT INTO settings (key, value) VALUES ('k', 'v')")
    db.init(conn)
    assert conn.execute("SELECT value FROM settings WHERE key = 'k'").fetchone()["value"] == "v"


def test_new_id_is_prefix_plus_sixteen_hex_and_unique():
    # Bug caught: ids that collide or that do not carry their kind, making a proposal id usable as a payout id.
    first, second = db.new_id("prop"), db.new_id("prop")
    assert re.fullmatch(r"prop_[0-9a-f]{16}", first)
    assert first != second


# --- db.transaction ------------------------------------------------------------------------


def test_transaction_commits_so_another_connection_sees_it(db_path, conn):
    # Bug caught: a transaction that never commits, so writes vanish when the connection closes.
    with db.transaction(conn):
        conn.execute("INSERT INTO settings (key, value) VALUES ('k', 'v')")
    other = db.connect(db_path)
    try:
        assert other.execute("SELECT value FROM settings").fetchone()["value"] == "v"
    finally:
        other.close()


def test_transaction_rolls_back_on_an_exception_and_reraises(conn):
    # Bug caught: half of a money change surviving after a later step failed.
    with pytest.raises(RuntimeError, match="boom"):
        with db.transaction(conn):
            conn.execute("INSERT INTO settings (key, value) VALUES ('k', 'v')")
            raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM settings").fetchone()[0] == 0
    assert not conn.in_transaction


def test_nested_transaction_joins_the_outer_one(conn):
    # Bug caught: an inner block committing early, so an outer failure cannot undo it.
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            with db.transaction(conn):
                conn.execute("INSERT INTO settings (key, value) VALUES ('k', 'v')")
            assert conn.in_transaction
            raise RuntimeError("outer fails after inner finished")
    assert conn.execute("SELECT count(*) FROM settings").fetchone()[0] == 0


def test_original_error_survives_when_sqlite_already_rolled_back(conn):
    # Bug caught: a "no transaction is active" error from ROLLBACK hiding the real failure.
    with pytest.raises(RuntimeError, match="the real failure"):
        with db.transaction(conn):
            conn.execute("ROLLBACK")
            raise RuntimeError("the real failure")


# --- audit: append-only --------------------------------------------------------------------


def test_audit_rows_cannot_be_updated_or_deleted(conn):
    # Bug caught: history being rewritten through the app's own connection.
    record(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit SET event = 'other'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit")


# --- audit: the hash chain -----------------------------------------------------------------


def test_record_returns_increasing_seq_and_first_row_starts_at_genesis(conn):
    # Bug caught: the first row not anchored to GENESIS, or seq not matching the stored row.
    first = record(conn, "a")
    second = record(conn, "b")
    assert second > first
    rows = audit_rows(conn)
    assert [r["seq"] for r in rows] == [first, second]
    assert rows[0]["prev_hash"] == audit.GENESIS
    assert rows[1]["prev_hash"] == rows[0]["hash"]


def test_hash_is_sha256_of_previous_hash_and_canonical_json(conn):
    # Bug caught: changing which fields are hashed, or their order, silently invalidating every existing log.
    record(conn, "paid", data={"b": 1, "a": 2})
    row = audit_rows(conn)[0]
    canonical = json.dumps(
        {"at": "2026-10-19T09:00:00+00:00", "family_id": "fam-1", "actor": "system", "event": "paid", "data": {"b": 1, "a": 2}},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    assert row["hash"] == hashlib.sha256((audit.GENESIS + canonical).encode()).hexdigest()


def test_verify_is_true_for_an_empty_log_and_after_several_records(conn):
    # Bug caught: verify failing on a fresh database, or rejecting a log that was written correctly.
    assert audit.verify(conn) == (True, None)
    for n in range(5):
        record(conn, f"e{n}", family_id=None if n == 2 else "fam-1", data={"n": n})
    assert audit.verify(conn) == (True, None)


def test_verify_still_true_when_data_holds_non_json_values_and_unicode(conn):
    # Bug caught: verify re-reading the stored text differently from how it was hashed (datetimes, accents).
    record(conn, data={"when": NOW, "name": "Amina Ōtieno", "amount": 12.5, "ok": True, "none": None})
    assert audit.verify(conn) == (True, None)


def test_verify_detects_an_edit_made_behind_the_apps_back(db_path, conn):
    # Bug caught: a tampered row going unnoticed because only the app's own writes were guarded.
    record(conn, "a")
    target = record(conn, "b", data={"amount": 100})
    record(conn, "c")
    attacker = db.connect(db_path)
    try:
        attacker.execute("DROP TRIGGER audit_no_update")
        attacker.execute("UPDATE audit SET data_json = ? WHERE seq = ?", ('{"amount":999999}', target))
    finally:
        attacker.close()
    assert audit.verify(conn) == (False, target)


def test_verify_detects_a_deleted_row_and_names_the_row_after_the_gap(db_path, conn):
    # Bug caught: a removed row going unnoticed because the surviving rows still hash correctly on their own.
    record(conn, "a")
    removed = record(conn, "b")
    after_gap = record(conn, "c")
    attacker = db.connect(db_path)
    try:
        attacker.execute("DROP TRIGGER audit_no_delete")
        attacker.execute("DELETE FROM audit WHERE seq = ?", (removed,))
    finally:
        attacker.close()
    assert audit.verify(conn) == (False, after_gap)


def test_verify_detects_a_rewritten_link_to_the_previous_row(db_path, conn):
    # Bug caught: the stored prev_hash being edited to point elsewhere while the row's own hash still looks fine.
    record(conn, "a")
    target = record(conn, "b")
    attacker = db.connect(db_path)
    try:
        attacker.execute("DROP TRIGGER audit_no_update")
        attacker.execute("UPDATE audit SET prev_hash = ? WHERE seq = ?", (audit.GENESIS, target))
    finally:
        attacker.close()
    assert audit.verify(conn) == (False, target)


@pytest.mark.parametrize(
    "garbage",
    [
        pytest.param("not json", id="text-that-is-not-json"),
        pytest.param(b"\xff\xfe\xfa", id="bytes-that-are-not-text"),
        pytest.param("[" * 100_000 + "]" * 100_000, id="absurdly-deep-nesting"),
    ],
)
def test_verify_reports_a_row_whose_stored_data_cannot_be_read(db_path, conn, garbage):
    # Bug caught: verify crashing on a tampered row's unreadable data instead of returning (False, seq) for it.
    record(conn, "a")
    target = record(conn, "b", data={"amount": 100})
    record(conn, "c")
    attacker = db.connect(db_path)
    try:
        attacker.execute("DROP TRIGGER audit_no_update")
        attacker.execute("UPDATE audit SET data_json = ? WHERE seq = ?", (garbage, target))
    finally:
        attacker.close()
    assert audit.verify(conn) == (False, target)


@pytest.mark.parametrize("column", ["data_json", "prev_hash", "hash", "actor", "event", "at", "family_id"])
def test_verify_reports_a_row_with_invalid_utf8_in_any_column(db_path, conn, column):
    # Bug caught: verify crashing while fetching a row whose TEXT column holds bytes that are not valid UTF-8.
    record(conn, "a")
    target = record(conn, "b", data={"amount": 100})
    record(conn, "c")
    # A Python bind parameter cannot store invalid UTF-8 as TEXT; only a SQL CAST can.
    tamper(db_path, f"UPDATE audit SET {column} = CAST(X'FFFE' AS TEXT) WHERE seq = {target}")
    assert audit.verify(conn) == (False, target)


def test_chain_continues_across_a_fresh_connection(db_path):
    # Bug caught: keeping the chain head in memory, so every restart forked the chain from GENESIS.
    first_conn = db.connect(db_path)
    db.init(first_conn)
    record(first_conn, "before restart")
    first_conn.close()

    second_conn = db.connect(db_path)
    try:
        record(second_conn, "after restart")
        rows = audit_rows(second_conn)
        assert rows[1]["prev_hash"] == rows[0]["hash"]
        assert audit.verify(second_conn) == (True, None)
    finally:
        second_conn.close()


def test_record_inside_an_outer_transaction_rolls_back_with_it(conn):
    # Bug caught: the audit row committing on its own, leaving a log entry for a change that was undone.
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            record(conn, "payout started")
            raise RuntimeError("provider call failed")
    assert audit_rows(conn) == []
    assert audit.verify(conn) == (True, None)


def test_record_refuses_a_naive_datetime(conn):
    # Bug caught: a local-time stamp being logged as if it were UTC.
    with pytest.raises(ValueError):
        record(conn, at=datetime(2026, 10, 19, 9, 0))
    assert audit_rows(conn) == []


# --- audit: secrets are refused ------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"pin": "1234"},
        {"x": {"api_key": "k"}},
        {"confirm_token": "t"},
        {"pin_failures": 2},
        {"PIN": "1234"},
        {"Webhook_Secret": "s"},
        {"user_password": "p"},
        {"API_KEY": "k"},
        {"events": [{"detail": {"token": "t"}}]},
    ],
)
def test_record_refuses_secret_looking_keys_at_any_depth(conn, data):
    # Bug caught: a PIN, token or API key reaching the permanent log through a nested or oddly cased field.
    with pytest.raises(ValueError):
        record(conn, data=data)
    assert audit_rows(conn) == []


def test_record_allows_keys_that_only_look_similar(conn):
    # Bug caught: matching secret words as substrings, which would stop a PIN lockout (or an LLM token count) being logged.
    data = {"failures": 3, "locked_until": "2026-10-19T09:15:00+00:00", "tokens_used": 5, "pinned": True}
    seq = record(conn, data=data)
    assert json.loads(audit_rows(conn)[0]["data_json"]) == data
    assert seq == audit_rows(conn)[0]["seq"]


# --- audit.events --------------------------------------------------------------------------


def test_events_are_newest_first_with_parsed_data(conn):
    # Bug caught: the family's history showing oldest first, or handing back raw JSON text.
    record(conn, "first", data={"n": 1})
    record(conn, "second", data={"n": 2})
    events = audit.events(conn)
    assert [e["event"] for e in events] == ["second", "first"]
    assert events[0]["data"] == {"n": 2}
    assert events[0]["actor"] == "system"
    assert events[0]["family_id"] == "fam-1"


def test_events_for_one_family_never_include_another_familys_rows(conn):
    # Bug caught: one family reading another family's audit history.
    record(conn, "mine", family_id="fam-1")
    record(conn, "theirs", family_id="fam-2")
    record(conn, "system-wide", family_id=None)
    assert [e["event"] for e in audit.events(conn, "fam-1")] == ["mine"]


def test_events_limit_keeps_the_newest(conn):
    # Bug caught: a limit that cuts off the newest events instead of the oldest.
    for n in range(5):
        record(conn, f"e{n}")
    assert [e["event"] for e in audit.events(conn, limit=2)] == ["e4", "e3"]


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("data_json", "CAST(X'FFFE' AS TEXT)"),
        ("data_json", "'not json'"),
        ("actor", "CAST(X'FFFE' AS TEXT)"),
    ],
    ids=["data-invalid-utf8", "data-not-json", "other-column-invalid-utf8"],
)
def test_events_returns_a_tampered_row_as_unreadable_instead_of_raising(db_path, conn, column, value):
    # Bug caught: one tampered row crashing the whole log view, hiding the very tampering it should show.
    record(conn, "a", data={"n": 1})
    target = record(conn, "b", data={"n": 2})
    record(conn, "c", data={"n": 3})
    tamper(db_path, f"UPDATE audit SET {column} = {value} WHERE seq = {target}")
    events = audit.events(conn)
    assert [e["seq"] for e in events] == [3, target, 1]
    assert events[1]["data"] == {"unreadable": True}
    assert events[0]["data"] == {"n": 3} and events[2]["data"] == {"n": 1}
    assert {frozenset(e) for e in events} == {frozenset({"seq", "at", "family_id", "actor", "event", "data", "hash"})}

