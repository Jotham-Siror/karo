"""The background tick: what has to happen because time has passed.

For each family, in this order: payouts left pending are asked about again, PIN-confirmed requests that never got as
far as a payout are run, waiting requests whose wait is over are made again, the monthly allowance is paid, and late
shares are reminded. Money still moves only through the executor,
and the allowance only because the main payer's PIN already approved the standing order behind it.

An error from the provider or the network on one item is audited (by type only) and the item is left for the next
tick, so it never stops the items after it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from babel.dates import format_date

from karo import audit, clock, collections, db, executor, outbox, proposals, quotes, store
from karo.domain import Kind, StandingOrder
from karo.proposals import ProposalOutcome
from karo.providers.base import Provider
from karo.rules import Outcome

logger = logging.getLogger(__name__)

REMIND_AFTER = timedelta(hours=24)  # how long a payment link goes unpaid before the first reminder

# The demo button runs a tick while the background one may be running. The database stops two at once from making
# two allowance runs; this stops them both fetching a quote and asking the provider about the same payouts.
_ONE_TICK_AT_A_TIME = threading.Lock()


@dataclass(frozen=True)
class _Run:
    conn: sqlite3.Connection
    provider: Provider
    family_id: str
    now: datetime
    base_url: str


def tick(conn: sqlite3.Connection, provider: Provider, *, now: datetime, base_url: str) -> None:
    if conn.in_transaction:
        # Each step calls the provider, and a provider call must never be made while the write lock is held.
        raise RuntimeError("tick must not run inside a transaction.")
    with _ONE_TICK_AT_A_TIME:
        for row in conn.execute("SELECT id FROM families ORDER BY rowid").fetchall():
            run = _Run(conn, provider, row["id"], now, base_url)
            _reconcile_payouts(run)
            _run_confirmed(run)
            _renew_waiting(run)
            _pay_allowance(run)
            _remind_late_shares(run)


@contextmanager
def _isolated(run: _Run, step: str, subject_id: str) -> Iterator[None]:
    try:
        yield
    except Exception as error:
        logger.exception("scheduler step %s failed for %s", step, subject_id)
        audit.record(
            run.conn,
            at=run.now,
            family_id=run.family_id,
            actor="system",
            event="scheduler.error",
            data={"step": step, "subject_id": subject_id, "error": type(error).__name__},
        )


# --- payouts left pending ---------------------------------------------------------------------


def _reconcile_payouts(run: _Run) -> None:
    older_than = clock.iso(run.now - executor.RECONCILE_GRACE)
    rows = run.conn.execute(
        "SELECT id, proposal_id FROM payouts WHERE family_id = ? AND status = 'pending' AND updated_at < ? "
        "ORDER BY rowid",
        (run.family_id, older_than),
    ).fetchall()
    for row in rows:
        with _isolated(run, "payout", row["id"]):
            _execute(run, row["proposal_id"])


def _run_confirmed(run: _Run) -> None:
    """Runs what the PIN confirmed but the executor never got to commit: a lock timeout or a restart before its first
    commit leaves no payouts row for the step above to find. Within the grace the PIN page may still be running it.

    Only PIN-confirmed requests: an allowance run has no confirmation and `_run_order` picks it up within its own month.
    The executor moves anything it stops to blocked or waiting, and anything it starts gets a payouts row, so neither
    comes back here."""
    older_than = clock.iso(run.now - executor.RECONCILE_GRACE)
    rows = run.conn.execute(
        "SELECT p.id FROM proposals p JOIN confirmations c ON c.proposal_id = p.id "
        "WHERE p.family_id = ? AND p.status = 'confirmed' AND c.at < ? "
        "AND NOT EXISTS (SELECT 1 FROM payouts o WHERE o.proposal_id = p.id) ORDER BY c.at, p.rowid",
        (run.family_id, older_than),
    ).fetchall()
    for row in rows:
        with _isolated(run, "confirmed", row["id"]):
            _execute(run, row["id"])


def _execute(run: _Run, proposal_id: str) -> None:
    executor.execute(
        run.conn,
        run.provider,
        family_id=run.family_id,
        proposal_id=proposal_id,
        now=run.now,
        base_url=run.base_url,
    )


# --- requests that were waiting out a delay ---------------------------------------------------


def _renew_waiting(run: _Run) -> None:
    rows = run.conn.execute(
        "SELECT id, kind, created_at, decision_json FROM proposals "
        "WHERE family_id = ? AND status = 'waiting' ORDER BY created_at, rowid",
        (run.family_id,),
    ).fetchall()
    for row in rows:
        ends = _wait_ends(row["decision_json"])
        if ends is not None and run.now >= ends:
            with _isolated(run, "waiting", row["id"]):
                _renew_or_lapse(run, row)


def _month_of(moment: datetime, tz: ZoneInfo) -> tuple[int, int]:
    local = moment.astimezone(tz)
    return local.year, local.month


def _month_name(month: tuple[int, int]) -> str:
    # In English whatever the server's locale is: the message is for the main payer's chat.
    return format_date(date(*month, 1), "MMMM", locale="en")


def _renew_or_lapse(run: _Run, row: sqlite3.Row) -> None:
    tz = store.family_config(run.conn, run.family_id).timezone
    made_in = _month_of(clock.parse(row["created_at"]), tz)
    if row["kind"] == Kind.ALLOWANCE_RUN and made_in < _month_of(run.now, tz):
        _lapse(run, row["id"], made_in, tz)
    else:
        _renew(run, row["id"])


def _lapse(run: _Run, old_id: str, month: tuple[int, int], tz: ZoneInfo) -> None:
    """Skips an allowance run that waited past its own month. Paying it now would fill this month's slot, or sit
    beside this month's own run, so a month's allowance would be missed or paid twice."""
    with db.transaction(run.conn):
        store.update_proposal(run.conn, run.family_id, old_id, status="failed", now=run.now)
        audit.record(
            run.conn,
            at=run.now,
            family_id=run.family_id,
            actor="system",
            event="allowance.lapsed",
            data={"month": f"{month[0]}-{month[1]:02d}", "proposal_id": old_id},
        )
        missed, current = _month_name(month), _month_name(_month_of(run.now, tz))
        _say(run, f"{missed}'s allowance didn't run in time, so it was skipped. {current}'s runs as usual.")


def _wait_ends(decision_json: str) -> datetime | None:
    ends = [clock.parse(r["until"]) for r in json.loads(decision_json) if r["outcome"] == Outcome.WAIT and r["until"]]
    return max(ends, default=None)


def _renew(run: _Run, old_id: str) -> None:
    """Asks again for what was waiting. The old quote ran out long ago, and a PIN only approves one exact proposal,
    so the new request gets a new quote and, if it needs one, a new PIN link."""
    old, _ = store.get_proposal(run.conn, run.family_id, old_id)
    quote_id = None
    if old.quote is not None:
        quote = quotes.create_quote(
            run.conn,
            run.provider,
            family_id=run.family_id,
            source_currency=old.quote.source.currency,
            target=old.quote.target,
            now=run.now,
        )
        quote_id = quote.id
    # One transaction, so a crash can't leave a replacement with the old request still waiting: that would be
    # replaced again, and the main payer sent a second link.
    with db.transaction(run.conn):
        outcome = proposals.propose(
            run.conn,
            family_id=run.family_id,
            actor=store.member(run.conn, run.family_id, old.requested_by),
            kind=old.kind,
            now=run.now,
            base_url=run.base_url,
            amount=old.amount,
            payee=old.payee,
            quote_id=quote_id,
            share_id=old.share_id,
            standing_order_id=old.standing_order_id,
            fee_id=old.fee_id,
            split=old.split,
        )
        store.update_proposal(run.conn, run.family_id, old_id, status="failed", now=run.now)
        audit.record(
            run.conn,
            at=run.now,
            family_id=run.family_id,
            actor="system",
            event="proposal.superseded",
            data={"old_id": old_id, "new_id": outcome.proposal_id},
        )
    if outcome.status == "awaiting_pin":
        what = _what(run, outcome)
        _say(
            run,
            f"The wait is over for: {what}. The price you approved has expired, so Karo needs a new PIN "
            "confirmation. A new link was just sent.",
        )
    elif outcome.status == "confirmed":
        _execute(run, outcome.proposal_id)
    else:
        _explain_stop(run, outcome)


def _what(run: _Run, outcome: ProposalOutcome) -> str:
    p, _ = store.get_proposal(run.conn, run.family_id, outcome.proposal_id)
    return proposals.summary(p, store.load_state(run.conn, run.family_id, run.now))


def _say(run: _Run, text: str) -> None:
    outbox.notify(run.conn, store.main_payer(run.conn, run.family_id).id, text, run.now)


def _explain_stop(run: _Run, outcome: ProposalOutcome) -> None:
    """Tells the main payer why a request made by the scheduler is not running, in the executor's own words."""
    tz = store.family_config(run.conn, run.family_id).timezone
    _say(run, proposals.stop_notice(_what(run, outcome), outcome.decision, tz))


# --- the monthly allowance --------------------------------------------------------------------


def _pay_allowance(run: _Run) -> None:
    config = store.family_config(run.conn, run.family_id)
    local = run.now.astimezone(config.timezone)
    window = _month_window(local)
    if _already_paid(run, window):
        return
    for order in store.load_state(run.conn, run.family_id, run.now).standing_orders:
        if _is_due(run, order, local):
            with _isolated(run, "allowance", order.id):
                _run_order(run, order, config.policy.home_currency, window)


def _is_due(run: _Run, order: StandingOrder, local: datetime) -> bool:
    """The order's day has come this month, and that day is after the day the PIN approved the order.

    The main payer approved "on the 1st of each month", so the first run is the first such day after she approved it,
    never the day itself or an earlier day of the same month."""
    if local.day < order.day_of_month:
        return False
    approved = _approved_at(run, order.id)
    if approved is None:
        return True
    return date(local.year, local.month, order.day_of_month) > approved.astimezone(local.tzinfo).date()


def _approved_at(run: _Run, order_id: str) -> datetime | None:
    """When the PIN approved the order, or when it was proposed if no confirmation is recorded. None for an order
    with no proposal behind it at all."""
    row = run.conn.execute(
        "SELECT COALESCE(c.at, p.created_at) AS at FROM standing_orders o "
        "JOIN proposals p ON p.id = o.proposal_id AND p.family_id = o.family_id "
        "LEFT JOIN confirmations c ON c.proposal_id = p.id "
        "WHERE o.id = ? AND o.family_id = ?",
        (order_id, run.family_id),
    ).fetchone()
    return None if row is None else clock.parse(row["at"])


def _month_window(local: datetime) -> tuple[str, str]:
    """The family's local month as stored timestamps: from its start up to, not including, the next one."""
    first = local.date().replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    start = datetime.combine(first, time.min, tzinfo=local.tzinfo)
    end = datetime.combine(following, time.min, tzinfo=local.tzinfo)
    return clock.iso(start), clock.iso(end)


def _already_paid(run: _Run, window: tuple[str, str]) -> bool:
    row = run.conn.execute(
        "SELECT 1 FROM payouts WHERE family_id = ? AND kind = 'allowance_run' AND created_at >= ? AND created_at < ?",
        (run.family_id, *window),
    ).fetchone()
    return row is not None


def _run_order(run: _Run, order: StandingOrder, home_currency: str, window: tuple[str, str]) -> None:
    # A run made this month counts as the month's attempt whatever became of it, or a run the cap stops would be
    # made and announced again every 30 seconds. One a crash left confirmed is the exception: nothing came of it yet.
    attempt = _attempt(run, order, window)
    if attempt is None:
        outcome = _propose_run(run, order, home_currency, window)
        if outcome is None:
            return
        if outcome.status != "confirmed":
            _explain_stop(run, outcome)
            return
        proposal_id = outcome.proposal_id
    elif attempt["status"] == "confirmed":
        proposal_id = attempt["id"]
    else:
        return
    _execute(run, proposal_id)


def _attempt(run: _Run, order: StandingOrder, window: tuple[str, str]) -> sqlite3.Row | None:
    return run.conn.execute(
        "SELECT id, status FROM proposals WHERE family_id = ? AND kind = ? AND created_at >= ? AND created_at < ? "
        "AND json_extract(payload_json, '$.standing_order_id') = ? ORDER BY rowid DESC LIMIT 1",
        (run.family_id, Kind.ALLOWANCE_RUN.value, *window, order.id),
    ).fetchone()


def _propose_run(
    run: _Run, order: StandingOrder, home_currency: str, window: tuple[str, str]
) -> ProposalOutcome | None:
    """None if another tick made this month's run while the quote was being fetched; that tick runs it."""
    quote = quotes.create_quote(
        run.conn,
        run.provider,
        family_id=run.family_id,
        source_currency=home_currency,
        target=order.amount,
        now=run.now,
    )
    # The quote is a provider call, so it is fetched before the write lock is taken. Looking again for this month's run
    # under that lock is what stops two processes ticking at once from each making one.
    with db.transaction(run.conn):
        if _attempt(run, order, window) is not None:
            return None
        # Proposed as the main payer, the one member the rules accept for a run, so the proposal log names her. The
        # executor runs it with nobody behind it, as the scheduler's, and this entry says so.
        outcome = proposals.propose(
            run.conn,
            family_id=run.family_id,
            actor=store.main_payer(run.conn, run.family_id),
            kind=Kind.ALLOWANCE_RUN,
            now=run.now,
            base_url=run.base_url,
            amount=quote.target,
            payee=order.payee,
            quote_id=quote.id,
            standing_order_id=order.id,
        )
        audit.record(
            run.conn,
            at=run.now,
            family_id=run.family_id,
            actor="system",
            event="allowance.scheduled",
            data={"standing_order_id": order.id, "proposal_id": outcome.proposal_id},
        )
    return outcome


# --- reminders --------------------------------------------------------------------------------


def _remind_late_shares(run: _Run) -> None:
    for fee_id, share_id in _late_shares(run):
        with _isolated(run, "reminder", share_id):
            collections.send_reminders(
                run.conn,
                run.provider,
                family_id=run.family_id,
                fee_id=fee_id,
                share_id=share_id,
                now=run.now,
                base_url=run.base_url,
            )


def _late_shares(run: _Run) -> Iterator[tuple[str, str]]:
    """(fee id, share id) of each share still owing whose link is a day old. `send_reminders` skips anyone already
    reminded today."""
    for fee in store.fees(run.conn, run.family_id):
        for share in store.shares_for_fee(run.conn, run.family_id, fee["id"]):
            if share["paid_minor"] >= share["amount_minor"]:
                continue
            link = store.latest_pending_collection(run.conn, run.family_id, share["id"])
            if link is None or clock.parse(link["created_at"]) > run.now - REMIND_AFTER:
                continue
            yield fee["id"], share["id"]
