"""The ledger (append-only money movements), the demo clock, and the daily/monthly outflow sums.

Every test names the bug it would catch. Payout rows are inserted directly with SQL: the executor
that normally writes them comes later.
"""

import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import time_machine

from karo import clock, db, ledger
from karo.money import Money

NAIROBI = ZoneInfo("Africa/Nairobi")
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam-1"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "karo.db"


@pytest.fixture
def conn(db_path):
    connection = db.connect(db_path)
    db.init(connection)
    add_family(connection, FAM)
    yield connection
    connection.close()


def add_family(conn, family_id):
    conn.execute(
        "INSERT INTO families (id, name, home_country, home_currency, timezone, policy_json) "
        "VALUES (?, 'Test family', 'KE', 'KES', 'Africa/Nairobi', '{}')",
        (family_id,),
    )


def add_payout(conn, created_at, home_minor, *, family_id=FAM, status="succeeded", kind="pay_fee"):
    stamp = clock.iso(created_at)
    proposal_id = db.new_id("prop")
    conn.execute(
        "INSERT INTO proposals (id, family_id, kind, requested_by, payload_json, digest, status, decision_json, "
        "created_at, updated_at) VALUES (?, ?, ?, 'm1', '{}', 'd', 'executed', '{}', ?, ?)",
        (proposal_id, family_id, kind, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO payouts (id, proposal_id, family_id, provider, reference, amount_minor, currency, debit_minor, "
        "debit_currency, home_value_minor, kind, status, simulated, created_at, updated_at) "
        "VALUES (?, ?, ?, 'sim', ?, 1, 'GBP', ?, 'KES', ?, ?, ?, 1, ?, ?)",
        (db.new_id("pay"), proposal_id, family_id, db.new_id("ref"), home_minor, home_minor, kind, status, stamp, stamp),
    )


def kes(minor):
    return Money(minor, "KES")


def outflow(conn, day, family_id=FAM):
    return ledger.outflow_on(conn, family_id, day=day, tz=NAIROBI, home_currency="KES")


def allowance(conn, year, month, family_id=FAM):
    return ledger.allowance_in_month(conn, family_id, year=year, month=month, tz=NAIROBI, home_currency="KES")


# --- clock ---------------------------------------------------------------------------------


@time_machine.travel(NOW, tick=False)
def test_now_is_real_time_until_the_clock_is_moved(conn):
    # Bug caught: now() returning a naive time, or drifting from real time before any demo jump.
    assert clock.now(conn) == NOW
    assert clock.now(conn).tzinfo is not None
    assert clock.offset(conn) == 0


@time_machine.travel(NOW, tick=False)
def test_advance_moves_now_and_reset_returns_it(conn):
    # Bug caught: the demo clock jump not reaching now(), or reset leaving a stale offset behind.
    clock.advance(conn, 86_400)
    assert clock.now(conn) == NOW + timedelta(days=1)
    clock.reset(conn)
    assert clock.now(conn) == NOW
    assert clock.offset(conn) == 0


def test_advance_accumulates(conn):
    # Bug caught: each advance replacing the offset instead of adding to it.
    clock.advance(conn, 86_400)
    clock.advance(conn, 3_600)
    assert clock.offset(conn) == 90_000


def test_the_offset_survives_a_new_connection(db_path, conn):
    # Bug caught: keeping the offset in memory, so a server restart in the middle of a demo snapped time back.
    clock.advance(conn, 7_200)
    other = db.connect(db_path)
    try:
        assert clock.offset(other) == 7_200
    finally:
        other.close()


def test_iso_refuses_a_naive_datetime():
    # Bug caught: a local-time stamp being stored as if it were UTC.
    with pytest.raises(ValueError):
        clock.iso(datetime(2026, 10, 19, 9, 0))


def test_iso_converts_to_utc():
    # Bug caught: stamps stored in mixed offsets, which sort wrongly as text.
    nairobi_noon = datetime(2026, 10, 19, 12, 0, tzinfo=timezone(timedelta(hours=3)))
    assert clock.iso(nairobi_noon) == "2026-10-19T09:00:00+00:00"


def test_parse_reads_back_what_iso_wrote():
    # Bug caught: a round trip that loses the zone or the microseconds.
    moment = datetime(2026, 10, 19, 9, 0, 0, 123456, tzinfo=UTC)
    assert clock.parse(clock.iso(moment)) == moment
    assert clock.parse(clock.iso(moment)).utcoffset() == timedelta(0)


def test_parse_normalises_other_offsets_to_utc():
    # Bug caught: a stamp with an offset coming back in that offset and being compared as wall-clock time.
    parsed = clock.parse("2026-10-19T12:00:00+03:00")
    assert parsed == NOW
    assert parsed.utcoffset() == timedelta(0)


def test_parse_refuses_a_stamp_with_no_zone():
    # Bug caught: a naive stamp being guessed as UTC.
    with pytest.raises(ValueError):
        clock.parse("2026-10-19T09:00:00")


# --- ledger --------------------------------------------------------------------------------


def test_ledger_rows_cannot_be_updated_or_deleted(conn):
    # Bug caught: a money movement being edited or removed after the fact.
    ledger.post(conn, family_id=FAM, at=NOW, amount=kes(100), kind="collection", ref="c1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE ledger SET amount_minor = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM ledger")


def test_post_returns_increasing_seq(conn):
    # Bug caught: post returning nothing, so callers cannot cite the row they just wrote.
    first = ledger.post(conn, family_id=FAM, at=NOW, amount=kes(100), kind="collection", ref="c1")
    second = ledger.post(conn, family_id=FAM, at=NOW, amount=kes(-40), kind="payout", ref="p1")
    assert second > first


def test_post_refuses_a_naive_datetime(conn):
    # Bug caught: a ledger row stamped in local time.
    with pytest.raises(ValueError):
        ledger.post(conn, family_id=FAM, at=datetime(2026, 10, 19, 9, 0), amount=kes(1), kind="collection", ref="c1")


def test_balances_sum_per_currency_sorted_and_skip_zero(conn):
    # Bug caught: adding KES to GBP, keeping zero balances, or returning currencies in random order.

    def post(minor, currency, kind, ref):
        ledger.post(conn, family_id=FAM, at=NOW, amount=Money(minor, currency), kind=kind, ref=ref)

    post(500_000, "KES", "collection", "c1")
    post(-120_000, "KES", "payout", "p1")
    post(30_000, "GBP", "conversion", "v1")
    post(70_000, "UGX", "collection", "c2")
    post(-70_000, "UGX", "payout_reversal", "r1")
    assert ledger.balances(conn, FAM) == (Money(30_000, "GBP"), Money(380_000, "KES"))
    assert ledger.balance(conn, FAM, "KES") == Money(380_000, "KES")
    assert ledger.balance(conn, FAM, "UGX") == Money(0, "UGX")
    assert ledger.balance(conn, FAM, "USD") == Money(0, "USD")


def test_balances_never_include_another_familys_money(conn):
    # Bug caught: a missing family filter showing one family another family's balance.
    add_family(conn, "fam-2")
    ledger.post(conn, family_id=FAM, at=NOW, amount=kes(100), kind="collection", ref="c1")
    ledger.post(conn, family_id="fam-2", at=NOW, amount=kes(9_999), kind="collection", ref="c2")
    assert ledger.balances(conn, FAM) == (kes(100),)
    assert ledger.balance(conn, FAM, "KES") == kes(100)


# --- outflow_on ----------------------------------------------------------------------------


def test_outflow_counts_by_the_families_local_day_not_utc(conn):
    # Bug caught: grouping by UTC date, so a 23:30 UTC payout (02:30 next day in Nairobi) counted against the wrong day's cap.
    add_payout(conn, datetime(2026, 10, 19, 23, 30, tzinfo=UTC), 1_000)
    assert outflow(conn, date(2026, 10, 19)) == kes(0)
    assert outflow(conn, date(2026, 10, 20)) == kes(1_000)


def test_outflow_day_starts_at_local_midnight_exactly(conn):
    # Bug caught: an off-by-one at the day boundary (midnight in Nairobi is 21:00 UTC the evening before).
    add_payout(conn, datetime(2026, 10, 19, 20, 59, 59, tzinfo=UTC), 1)
    add_payout(conn, datetime(2026, 10, 19, 21, 0, 0, tzinfo=UTC), 10)
    add_payout(conn, datetime(2026, 10, 20, 20, 59, 59, 999999, tzinfo=UTC), 100)
    add_payout(conn, datetime(2026, 10, 20, 21, 0, 0, tzinfo=UTC), 1_000)
    assert outflow(conn, date(2026, 10, 19)) == kes(1)
    assert outflow(conn, date(2026, 10, 20)) == kes(110)
    assert outflow(conn, date(2026, 10, 21)) == kes(1_000)


def test_outflow_ignores_failed_payouts_but_counts_pending_and_succeeded(conn):
    # Bug caught: a failed payout still eating the daily cap, or a pending one escaping it.
    moment = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    add_payout(conn, moment, 1, status="succeeded")
    add_payout(conn, moment, 10, status="pending")
    add_payout(conn, moment, 100, status="failed")
    assert outflow(conn, date(2026, 10, 19)) == kes(11)


def test_outflow_counts_fee_payments_and_allowance_runs_together(conn):
    # Bug caught: the daily cap looking only at one kind of payout.
    moment = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    add_payout(conn, moment, 5, kind="pay_fee")
    add_payout(conn, moment, 7, kind="allowance_run")
    assert outflow(conn, date(2026, 10, 19)) == kes(12)


def test_outflow_never_includes_another_familys_payouts(conn):
    # Bug caught: a missing family filter letting one family's spending count against another's cap.
    add_family(conn, "fam-2")
    moment = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    add_payout(conn, moment, 5)
    add_payout(conn, moment, 9_000, family_id="fam-2")
    assert outflow(conn, date(2026, 10, 19)) == kes(5)
    assert outflow(conn, date(2026, 10, 19), family_id="fam-2") == kes(9_000)


def test_outflow_with_no_payouts_is_zero_in_the_home_currency(conn):
    # Bug caught: returning None or a zero in the wrong currency, which would break the cap comparison.
    assert outflow(conn, date(2026, 10, 19)) == Money(0, "KES")


# --- allowance_in_month --------------------------------------------------------------------


def test_allowance_counts_only_allowance_runs(conn):
    # Bug caught: school fees eating the student's monthly allowance cap.
    moment = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    add_payout(conn, moment, 3, kind="allowance_run")
    add_payout(conn, moment, 1_000, kind="pay_fee")
    assert allowance(conn, 2026, 10) == kes(3)


def test_allowance_ignores_failed_runs_and_other_families(conn):
    # Bug caught: a failed allowance run, or another family's, using up this month's cap.
    add_family(conn, "fam-2")
    moment = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    add_payout(conn, moment, 3, kind="allowance_run")
    add_payout(conn, moment, 30, kind="allowance_run", status="failed")
    add_payout(conn, moment, 300, kind="allowance_run", family_id="fam-2")
    assert allowance(conn, 2026, 10) == kes(3)


def test_allowance_month_follows_local_time(conn):
    # Bug caught: using UTC month edges, so a run at 00:30 on the 1st in Nairobi counted against the previous month.
    add_payout(conn, datetime(2026, 9, 30, 20, 59, 59, tzinfo=UTC), 1, kind="allowance_run")
    add_payout(conn, datetime(2026, 9, 30, 21, 0, 0, tzinfo=UTC), 10, kind="allowance_run")
    add_payout(conn, datetime(2026, 10, 31, 20, 59, 59, tzinfo=UTC), 100, kind="allowance_run")
    add_payout(conn, datetime(2026, 10, 31, 21, 0, 0, tzinfo=UTC), 1_000, kind="allowance_run")
    assert allowance(conn, 2026, 9) == kes(1)
    assert allowance(conn, 2026, 10) == kes(110)
    assert allowance(conn, 2026, 11) == kes(1_000)


def test_allowance_december_rolls_into_january(conn):
    # Bug caught: month 12 + 1 = 13 crashing, or December's sum spilling into January.
    add_payout(conn, datetime(2026, 12, 15, 9, 0, tzinfo=UTC), 5, kind="allowance_run")
    add_payout(conn, datetime(2026, 12, 31, 21, 30, tzinfo=UTC), 50, kind="allowance_run")
    assert allowance(conn, 2026, 12) == kes(5)
    assert allowance(conn, 2027, 1) == kes(50)
