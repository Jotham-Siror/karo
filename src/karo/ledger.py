"""The append-only ledger of real money movements, plus the payout sums the spending caps need."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from karo.clock import iso
from karo.money import Money


def post(conn: sqlite3.Connection, *, family_id: str, at: datetime, amount: Money, kind: str, ref: str) -> int:
    """`amount.minor` is signed: positive for money in, negative for money out."""
    cursor = conn.execute(
        "INSERT INTO ledger (family_id, at, currency, amount_minor, kind, ref) VALUES (?, ?, ?, ?, ?, ?)",
        (family_id, iso(at), amount.currency, amount.minor, kind, ref),
    )
    return cursor.lastrowid


def balances(conn: sqlite3.Connection, family_id: str) -> tuple[Money, ...]:
    rows = conn.execute(
        "SELECT currency, SUM(amount_minor) AS total FROM ledger WHERE family_id = ? "
        "GROUP BY currency HAVING total != 0 ORDER BY currency",
        (family_id,),
    )
    return tuple(Money(r["total"], r["currency"]) for r in rows)


def balance(conn: sqlite3.Connection, family_id: str, currency: str) -> Money:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_minor), 0) AS total FROM ledger WHERE family_id = ? AND currency = ?",
        (family_id, currency),
    ).fetchone()
    return Money(row["total"], currency)


def _payout_total(
    conn: sqlite3.Connection,
    family_id: str,
    *,
    first_day: date,
    after_last_day: date,
    tz: ZoneInfo,
    home_currency: str,
    kind: str | None = None,
) -> Money:
    """Sum of payouts created from local midnight on `first_day` up to local midnight on `after_last_day`."""
    start = iso(datetime.combine(first_day, time.min, tzinfo=tz).astimezone(UTC))
    end = iso(datetime.combine(after_last_day, time.min, tzinfo=tz).astimezone(UTC))
    # created_at is always written by clock.iso (UTC, same format), so comparing it as text is comparing it as time.
    sql = (
        "SELECT COALESCE(SUM(home_value_minor), 0) AS total FROM payouts "
        "WHERE family_id = ? AND status != 'failed' AND created_at >= ? AND created_at < ?"
    )
    args: list[object] = [family_id, start, end]
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    return Money(conn.execute(sql, args).fetchone()["total"], home_currency)


def outflow_on(conn: sqlite3.Connection, family_id: str, *, day: date, tz: ZoneInfo, home_currency: str) -> Money:
    return _payout_total(
        conn, family_id, first_day=day, after_last_day=day + timedelta(days=1), tz=tz, home_currency=home_currency
    )


def allowance_in_month(
    conn: sqlite3.Connection, family_id: str, *, year: int, month: int, tz: ZoneInfo, home_currency: str
) -> Money:
    first = date(year, month, 1)
    following = date(year + month // 12, month % 12 + 1, 1)
    return _payout_total(
        conn,
        family_id,
        first_day=first,
        after_last_day=following,
        tz=tz,
        home_currency=home_currency,
        kind="allowance_run",
    )
