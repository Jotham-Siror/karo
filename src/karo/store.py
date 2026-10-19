"""Family-scoped reads and writes. Every function that touches family data takes `family_id` and filters by it.

The one exception is `member_for_identity`: it answers "who is this sender?", so it cannot know the family yet.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from karo import clock, db, ledger
from karo.domain import (
    FamilyState,
    Kind,
    Member,
    Payee,
    Policy,
    Proposal,
    Quote,
    Role,
    Share,
    StandingOrder,
    TrustedPayee,
)
from karo.money import Money
from karo.rules import Decision

# --- families and people ---------------------------------------------------------------------


@dataclass(frozen=True)
class FamilyConfig:
    policy: Policy
    timezone: ZoneInfo
    home_country: str
    quote_ttl: timedelta
    pin_max_attempts: int
    pin_lockout: timedelta


def family_row(conn: sqlite3.Connection, family_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM families WHERE id = ?", (family_id,)).fetchone()
    if row is None:
        raise LookupError(f"There is no family {family_id!r}.")
    return row


def _policy_money(raw: dict) -> Money:
    return Money(raw["minor"], raw["currency"])


def _config_from(row: sqlite3.Row) -> FamilyConfig:
    raw = json.loads(row["policy_json"])
    policy = Policy(
        home_currency=row["home_currency"],
        payment_cap=_policy_money(raw["payment_cap"]),
        allowance_monthly_cap=_policy_money(raw["allowance_monthly_cap"]),
        daily_cap=_policy_money(raw["daily_cap"]),
        new_payee_threshold=_policy_money(raw["new_payee_threshold"]),
        new_payee_wait=timedelta(seconds=raw["new_payee_wait_seconds"]),
    )
    return FamilyConfig(
        policy=policy,
        timezone=ZoneInfo(row["timezone"]),
        home_country=row["home_country"],
        quote_ttl=timedelta(seconds=raw["quote_ttl_seconds"]),
        pin_max_attempts=raw["pin_max_attempts"],
        pin_lockout=timedelta(seconds=raw["pin_lockout_seconds"]),
    )


def family_config(conn: sqlite3.Connection, family_id: str) -> FamilyConfig:
    return _config_from(family_row(conn, family_id))


def _member_from(row: sqlite3.Row) -> Member:
    return Member(row["id"], row["family_id"], Role(row["role"]), row["name"])


def members(conn: sqlite3.Connection, family_id: str) -> tuple[Member, ...]:
    rows = conn.execute(
        "SELECT id, family_id, role, name FROM members WHERE family_id = ? ORDER BY rowid", (family_id,)
    )
    return tuple(_member_from(r) for r in rows)


def member(conn: sqlite3.Connection, family_id: str, member_id: str) -> Member | None:
    row = conn.execute(
        "SELECT id, family_id, role, name FROM members WHERE family_id = ? AND id = ?", (family_id, member_id)
    ).fetchone()
    return None if row is None else _member_from(row)


def main_payer(conn: sqlite3.Connection, family_id: str) -> Member:
    row = conn.execute(
        "SELECT id, family_id, role, name FROM members "
        "WHERE family_id = ? AND role = 'main_payer' ORDER BY rowid LIMIT 1",
        (family_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Family {family_id!r} has no main payer.")
    return _member_from(row)


def member_for_identity(conn: sqlite3.Connection, channel: str, address: str) -> Member | None:
    row = conn.execute(
        "SELECT m.id, m.family_id, m.role, m.name FROM identities i JOIN members m ON m.id = i.member_id "
        "WHERE i.channel = ? AND i.address = ?",
        (channel, address),
    ).fetchone()
    return None if row is None else _member_from(row)


def identities_of(conn: sqlite3.Connection, member_id: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT channel, address FROM identities WHERE member_id = ? ORDER BY channel, address", (member_id,)
    )
    return [(r["channel"], r["address"]) for r in rows]


# --- the checker's view of a family ----------------------------------------------------------


def load_state(conn: sqlite3.Connection, family_id: str, now: datetime) -> FamilyState:
    # One transaction, so a payout landing mid-read can't leave balances and daily totals from different moments.
    with db.transaction(conn):
        row = family_row(conn, family_id)
        config = _config_from(row)
        local_now = now.astimezone(config.timezone)
        home = config.policy.home_currency
        return FamilyState(
            family_id=family_id,
            policy=config.policy,
            members=members(conn, family_id),
            trusted_payees=_trusted_payees(conn, family_id),
            shares=_shares(conn, family_id),
            standing_orders=_active_standing_orders(conn, family_id),
            balances=ledger.balances(conn, family_id),
            frozen=bool(row["frozen"]),
            pin_locked_until=None if row["pin_locked_until"] is None else clock.parse(row["pin_locked_until"]),
            outflow_today=ledger.outflow_on(
                conn, family_id, day=local_now.date(), tz=config.timezone, home_currency=home
            ),
            allowance_this_month=ledger.allowance_in_month(
                conn, family_id, year=local_now.year, month=local_now.month, tz=config.timezone, home_currency=home
            ),
        )


def _payee_from(name: str, details_json: str) -> Payee:
    return Payee.of(name, **json.loads(details_json))


def _trusted_payees(conn: sqlite3.Connection, family_id: str, source: str | None = None) -> tuple[TrustedPayee, ...]:
    rows = conn.execute(
        "SELECT id, family_id, name, details_json, trusted_since FROM trusted_payees "
        "WHERE family_id = ? AND (? IS NULL OR source = ?) ORDER BY rowid",
        (family_id, source, source),
    )
    return tuple(
        TrustedPayee(
            r["id"], r["family_id"], _payee_from(r["name"], r["details_json"]), clock.parse(r["trusted_since"])
        )
        for r in rows
    )


def added_payees(conn: sqlite3.Connection, family_id: str) -> tuple[TrustedPayee, ...]:
    """The trusted payees the family added itself, as opposed to the institutions from the directory."""
    return _trusted_payees(conn, family_id, "added")


def _shares(conn: sqlite3.Connection, family_id: str) -> tuple[Share, ...]:
    rows = conn.execute(
        "SELECT id, family_id, member_id, amount_minor, currency, paid_minor FROM shares "
        "WHERE family_id = ? ORDER BY rowid",
        (family_id,),
    )
    return tuple(
        Share(
            r["id"],
            r["family_id"],
            r["member_id"],
            Money(r["amount_minor"], r["currency"]),
            Money(r["paid_minor"], r["currency"]),
        )
        for r in rows
    )


def _active_standing_orders(conn: sqlite3.Connection, family_id: str) -> tuple[StandingOrder, ...]:
    rows = conn.execute(
        "SELECT o.id, o.family_id, o.amount_minor, o.currency, o.day_of_month, o.approved_by, t.name, t.details_json "
        "FROM standing_orders o JOIN trusted_payees t ON t.id = o.payee_id AND t.family_id = o.family_id "
        "WHERE o.family_id = ? AND o.active = 1 ORDER BY o.rowid",
        (family_id,),
    )
    return tuple(
        StandingOrder(
            r["id"],
            r["family_id"],
            _payee_from(r["name"], r["details_json"]),
            Money(r["amount_minor"], r["currency"]),
            r["day_of_month"],
            r["approved_by"],
            True,
        )
        for r in rows
    )


# --- quotes ----------------------------------------------------------------------------------


def save_quote(
    conn: sqlite3.Connection,
    *,
    family_id: str,
    provider: str,
    source: Money,
    target: Money,
    rate: Decimal,
    created_at: datetime,
    expires_at: datetime,
) -> Quote:
    quote_id = db.new_id("q")
    conn.execute(
        "INSERT INTO quotes (id, family_id, provider, source_minor, source_currency, target_minor, target_currency, "
        "rate, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            quote_id,
            family_id,
            provider,
            source.minor,
            source.currency,
            target.minor,
            target.currency,
            str(rate),
            clock.iso(created_at),
            clock.iso(expires_at),
        ),
    )
    return Quote(quote_id, family_id, source, target, expires_at)


def get_quote(conn: sqlite3.Connection, family_id: str, quote_id: str) -> Quote | None:
    row = conn.execute(
        "SELECT id, family_id, source_minor, source_currency, target_minor, target_currency, expires_at "
        "FROM quotes WHERE id = ? AND family_id = ?",
        (quote_id, family_id),
    ).fetchone()
    if row is None:
        return None
    return Quote(
        row["id"],
        row["family_id"],
        Money(row["source_minor"], row["source_currency"]),
        Money(row["target_minor"], row["target_currency"]),
        clock.parse(row["expires_at"]),
    )


# --- proposals -------------------------------------------------------------------------------


def _money_json(money: Money | None) -> dict | None:
    return None if money is None else {"minor": money.minor, "currency": money.currency}


def _money_from(raw: dict | None) -> Money | None:
    return None if raw is None else Money(raw["minor"], raw["currency"])


def _payee_json(payee: Payee | None) -> dict | None:
    return None if payee is None else {"name": payee.name, "details": [list(pair) for pair in payee.details]}


def _payee_from_json(raw: dict | None) -> Payee | None:
    # Built directly, not through Payee.of: the stored pairs are already in the order the digest was made from.
    return None if raw is None else Payee(raw["name"], tuple((k, v) for k, v in raw["details"]))


def _quote_json(quote: Quote | None) -> dict | None:
    if quote is None:
        return None
    return {
        "id": quote.id,
        "family_id": quote.family_id,
        "source": _money_json(quote.source),
        "target": _money_json(quote.target),
        "expires_at": clock.iso(quote.expires_at),
    }


def _quote_from_json(raw: dict | None) -> Quote | None:
    if raw is None:
        return None
    return Quote(
        raw["id"],
        raw["family_id"],
        _money_from(raw["source"]),
        _money_from(raw["target"]),
        clock.parse(raw["expires_at"]),
    )


def proposal_to_json(p: Proposal) -> str:
    return json.dumps(
        {
            "id": p.id,
            "family_id": p.family_id,
            "kind": p.kind.value,
            "requested_by": p.requested_by,
            "amount": _money_json(p.amount),
            "payee": _payee_json(p.payee),
            "quote": _quote_json(p.quote),
            "share_id": p.share_id,
            "standing_order_id": p.standing_order_id,
            "fee_id": p.fee_id,
            "split": [[member_id, _money_json(amount)] for member_id, amount in p.split],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def proposal_from_json(s: str) -> Proposal:
    raw = json.loads(s)
    return Proposal(
        id=raw["id"],
        family_id=raw["family_id"],
        kind=Kind(raw["kind"]),
        requested_by=raw["requested_by"],
        amount=_money_from(raw["amount"]),
        payee=_payee_from_json(raw["payee"]),
        quote=_quote_from_json(raw["quote"]),
        share_id=raw["share_id"],
        standing_order_id=raw["standing_order_id"],
        fee_id=raw["fee_id"],
        split=tuple((member_id, _money_from(amount)) for member_id, amount in raw["split"]),
    )


def _decision_json(decision: Decision) -> str:
    return json.dumps(
        [
            {
                "rule": r.rule,
                "outcome": r.outcome.value,
                "reason": r.reason,
                "until": None if r.until is None else clock.iso(r.until),
                "warn": r.warn,
            }
            for r in decision.results
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def insert_proposal(conn: sqlite3.Connection, p: Proposal, *, status: str, decision: Decision, now: datetime) -> None:
    stamp = clock.iso(now)
    conn.execute(
        "INSERT INTO proposals (id, family_id, kind, requested_by, payload_json, digest, status, decision_json, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            p.id,
            p.family_id,
            p.kind.value,
            p.requested_by,
            proposal_to_json(p),
            p.digest(),
            status,
            _decision_json(decision),
            stamp,
            stamp,
        ),
    )


def update_proposal(
    conn: sqlite3.Connection,
    family_id: str,
    proposal_id: str,
    *,
    status: str,
    decision: Decision | None = None,
    now: datetime,
) -> None:
    """Moves a proposal to a new status. The payload and digest never change: they are what the PIN approved."""
    cursor = conn.execute(
        "UPDATE proposals SET status = ?, decision_json = COALESCE(?, decision_json), updated_at = ? "
        "WHERE id = ? AND family_id = ?",
        (status, None if decision is None else _decision_json(decision), clock.iso(now), proposal_id, family_id),
    )
    if cursor.rowcount == 0:
        raise LookupError(f"Family {family_id!r} has no proposal {proposal_id!r}.")


def get_proposal(conn: sqlite3.Connection, family_id: str, proposal_id: str) -> tuple[Proposal, sqlite3.Row] | None:
    row = conn.execute("SELECT * FROM proposals WHERE id = ? AND family_id = ?", (proposal_id, family_id)).fetchone()
    return None if row is None else (proposal_from_json(row["payload_json"]), row)


def open_proposals(conn: sqlite3.Connection, family_id: str) -> list[tuple[Proposal, sqlite3.Row]]:
    """Proposals still on their way: held for a PIN, waiting out a delay, or approved and not yet run."""
    rows = conn.execute(
        "SELECT * FROM proposals WHERE family_id = ? AND status IN ('awaiting_pin', 'waiting', 'confirmed') "
        "ORDER BY created_at, rowid",
        (family_id,),
    ).fetchall()
    return [(proposal_from_json(r["payload_json"]), r) for r in rows]


# --- fees and shares -------------------------------------------------------------------------


def insert_fee(
    conn: sqlite3.Connection,
    *,
    family_id: str,
    payee: Payee,
    amount: Money,
    due_date: str | None,
    invoice_ref: str | None,
    now: datetime,
) -> str:
    """`due_date` is a YYYY-MM-DD date, kept as plain text (not a timestamp). None or "" means no due date."""
    due = date.fromisoformat(due_date).isoformat() if due_date else None
    fee_id = db.new_id("fee")
    conn.execute(
        "INSERT INTO fees (id, family_id, payee_name, payee_details_json, amount_minor, currency, due_date, "
        "invoice_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fee_id,
            family_id,
            payee.name,
            json.dumps(dict(payee.details), sort_keys=True),
            amount.minor,
            amount.currency,
            due,
            invoice_ref,
            clock.iso(now),
        ),
    )
    return fee_id


def get_fee(conn: sqlite3.Connection, family_id: str, fee_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM fees WHERE id = ? AND family_id = ?", (fee_id, family_id)).fetchone()


def fees(conn: sqlite3.Connection, family_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM fees WHERE family_id = ? ORDER BY created_at, rowid", (family_id,)).fetchall()


def shares_for_fee(conn: sqlite3.Connection, family_id: str, fee_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM shares WHERE family_id = ? AND fee_id = ? ORDER BY rowid", (family_id, fee_id)
    ).fetchall()
