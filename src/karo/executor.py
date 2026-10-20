"""The executor: re-checks the rules, then does what a confirmed proposal says. The only place money leaves.

A payout is two commits with the provider call between them, never inside either:
  1. rules re-checked, then the payouts row and the ledger debit commit together;
  2. the provider is called, or asked for the status if the row is left over from a crashed attempt;
  3. the provider's answer is written.
A provider error is not caught: the row stays pending and the retry reconciles it. Marking it failed here
would hand back a debit for money that may already have gone.

A split commits its shares first, then asks each relative for their payment link with no transaction open.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from karo import approvals, audit, collections, db, ledger, outbox, proposals, rules, store
from karo import clock
from karo.clock import iso
from karo.domain import CheckContext, Confirmation, FamilyState, Kind, Member, Payee, Proposal, Stage, TrustedPayee
from karo.money import Money
from karo.providers.base import PayoutResult, Provider, payout_name
from karo.rules import Decision, Outcome

logger = logging.getLogger(__name__)

ExecutionStatus = Literal["executed", "failed", "pending", "blocked", "waiting", "not_confirmed", "unsupported"]

ON_ITS_WAY = "On its way: "  # the result page makes this prefix its heading

LINKS_LATER = "Some payment links couldn't be sent yet; Karo will retry when you send reminders."

ALLOWANCE_DAY = 1  # the demo's "1st of the month"; a chosen day comes later

# A payout row the provider has no record of is probably still being sent by another execute. Younger than this,
# leave it alone; older, assume the call never left and send it again under the same reference.
RECONCILE_GRACE = timedelta(minutes=2)

_NOT_CONFIRMED = {
    "executed": "That has already been done.",
    "failed": "That payment failed, so it can't be run again. Start a new one.",
    "blocked": "That was blocked, so it can't run.",
    "waiting": "That is waiting, so it can't run yet.",
    "awaiting_pin": "That hasn't been confirmed with the PIN yet.",
}


@dataclass(frozen=True)
class ExecutionOutcome:
    status: ExecutionStatus
    message: str
    payout_id: str | None = None
    warning: bool = False  # done, but something is left for the main payer to read; the page must stay up


@dataclass(frozen=True)
class _PayoutJob:
    """What is left to do once the payouts row is committed."""

    proposal: Proposal
    payout_id: str
    reference: str
    provider_id: str | None
    payee: Payee | None  # the stored record to pay; None if the trusted payee is gone, which stops any re-send
    payee_name: str
    narration: str
    created_at: datetime
    is_new: bool  # False: the row is left from an earlier attempt, so ask the provider before anything else
    frozen: bool = False


@dataclass(frozen=True)
class _SplitDone:
    """The shares are committed. The payment links are asked for next, outside the transaction."""

    outcome: ExecutionOutcome
    fee_id: str


def execute(
    conn: sqlite3.Connection,
    provider: Provider,
    *,
    family_id: str,
    proposal_id: str,
    now: datetime,
    base_url: str,
) -> ExecutionOutcome:
    if conn.in_transaction:
        # Joined to an outer transaction, the payouts row would not be committed before the provider is called.
        raise RuntimeError("execute must not run inside a transaction.")
    step = _prepare(conn, payout_name(provider), family_id=family_id, proposal_id=proposal_id, now=now)
    if isinstance(step, _SplitDone):
        return _ask_for_shares(conn, provider, step, family_id=family_id, now=now, base_url=base_url)
    if isinstance(step, ExecutionOutcome):
        return step
    if step.is_new:
        result = _send(provider, step)
    else:
        result = provider.payout_status(reference=step.reference, provider_id=step.provider_id)
        if _provider_has_no_record(step, result):
            held = _hold_back(step, now)
            if held is not None:
                return held
            # Providers deduplicate by reference, so if the first call did land this pays nothing twice.
            result = _send(provider, step)
    return _settle(conn, step, result, now)


def _ask_for_shares(
    conn: sqlite3.Connection, provider: Provider, done: _SplitDone, *, family_id: str, now: datetime, base_url: str
) -> ExecutionOutcome:
    """Not inside _prepare's transaction: a link is a provider call, and the shares must be saved before it.

    The split is done whatever happens here. If the links fail partway the main payer is told, and sending reminders
    asks again for every share still without one."""
    try:
        collections.request_all(conn, provider, family_id=family_id, fee_id=done.fee_id, now=now, base_url=base_url)
    except Exception as error:
        logger.exception("payment links failed after a split")
        with db.transaction(conn):
            audit.record(
                conn,
                at=now,
                family_id=family_id,
                actor="system",
                event="collection.error",
                data={"fee_id": done.fee_id, "error": type(error).__name__},
            )
            outbox.notify(conn, store.main_payer(conn, family_id).id, LINKS_LATER, now)
        return replace(done.outcome, message=f"{done.outcome.message} {LINKS_LATER}", warning=True)
    return done.outcome


def _send(provider: Provider, job: _PayoutJob) -> PayoutResult:
    p = job.proposal
    return provider.payout(
        reference=job.reference,
        amount=p.amount,
        debit_currency=p.quote.source.currency,
        payee=job.payee,
        narration=job.narration,
    )


def _provider_has_no_record(job: _PayoutJob, result: PayoutResult) -> bool:
    # A provider that knows the transfer has an id for it. "Pending, no id" is "not found yet", never "failed".
    return result.status == "pending" and result.provider_id is None and job.provider_id is None


def _hold_back(job: _PayoutJob, now: datetime) -> ExecutionOutcome | None:
    """None means the payment may be sent again. Anything else is the answer to give instead."""
    if job.payee is None or now - job.created_at < RECONCILE_GRACE:
        return _pending(job)
    if job.frozen:
        # The debit stays and the row stays pending, so a retry after the unfreeze sends it.
        return ExecutionOutcome("blocked", "Karo is frozen, so this payment is on hold.", job.payout_id)
    return None


# --- step 1: checks, and for a payout the row and the debit -----------------------------------


def _prepare(
    conn: sqlite3.Connection, provider_name: str, *, family_id: str, proposal_id: str, now: datetime
) -> ExecutionOutcome | _PayoutJob | _SplitDone:
    # One transaction from the first read to the debit, so two executes can't both pass the funds check.
    with db.transaction(conn):
        found = store.get_proposal(conn, family_id, proposal_id)
        if found is None:
            return ExecutionOutcome("not_confirmed", "I can't find that proposal.")
        p, row = found
        if row["status"] != "confirmed":
            return ExecutionOutcome("not_confirmed", _NOT_CONFIRMED.get(row["status"], "That isn't confirmed."))

        state = store.load_state(conn, family_id, now)
        earlier = conn.execute(
            "SELECT * FROM payouts WHERE proposal_id = ? AND family_id = ?", (proposal_id, family_id)
        ).fetchone()
        if earlier is not None:
            # The rules already passed and the debit is posted. Asking the provider moves no money, and re-running
            # funds and the daily cap would count this same payment a second time and block the reconcile.
            return _reconcile_job(conn, p, state, earlier)

        confirmation = approvals.confirmation_for(conn, family_id, proposal_id)
        actor = _actor(conn, p, confirmation)
        by = "system" if confirmation is None else confirmation.member_id
        checked = rules.check(p, state, CheckContext(now, Stage.EXECUTE, actor, confirmation))
        decision = Decision(proposals.fee_blocks(conn, p, state) + checked.results)
        proposals.audit_decision(conn, p=p, stage=Stage.EXECUTE, decision=decision, actor=by, now=now)
        if decision.verdict is not Outcome.PASS:
            return _stopped(conn, p, state, decision, now)
        return _carry_out(conn, provider_name, p, state, by, now)


def _actor(conn: sqlite3.Connection, p: Proposal, confirmation: Confirmation | None) -> Member | None:
    if p.kind is Kind.ALLOWANCE_RUN:
        return None
    if p.kind not in rules.PIN_KINDS:
        return store.member(conn, p.family_id, p.requested_by)
    # No confirmation means no one to name; the rules block a PIN kind with no one behind it.
    return None if confirmation is None else store.member(conn, p.family_id, confirmation.member_id)


def _stopped(
    conn: sqlite3.Connection, p: Proposal, state: FamilyState, decision: Decision, now: datetime
) -> ExecutionOutcome:
    main = store.main_payer(conn, p.family_id)
    what = proposals.summary(p, state)
    # Not left `confirmed`: a payment stopped here, say by a freeze, needs a fresh PIN to run once it is lifted.
    status = "blocked" if decision.verdict is Outcome.BLOCK else "waiting"
    store.update_proposal(conn, p.family_id, p.id, status=status, decision=decision, now=now)
    tz = store.family_config(conn, p.family_id).timezone
    outbox.notify(conn, main.id, proposals.stop_notice(what, decision, tz), now)
    if decision.verdict is Outcome.BLOCK:
        return ExecutionOutcome("blocked", f"That was stopped. {proposals.block_reasons(decision)}")
    return ExecutionOutcome("waiting", f"Not yet. {proposals.wait_text(decision, tz)}")


def _carry_out(
    conn: sqlite3.Connection, provider_name: str, p: Proposal, state: FamilyState, by: str, now: datetime
) -> ExecutionOutcome | _PayoutJob | _SplitDone:
    match p.kind:
        case Kind.PAY_FEE | Kind.SEND | Kind.ALLOWANCE_RUN:
            return _start_payout(conn, provider_name, p, state, by, now)
        case Kind.SPLIT:
            return _split(conn, p, state, by, now)
        case Kind.ALLOWANCE_SET:
            return _allowance_set(conn, p, state, by, now)
        case Kind.ADD_PAYEE:
            return _add_payee(conn, p, state, by, now)
        case Kind.UNFREEZE:
            return _unfreeze(conn, p, state, by, now)
        case Kind.CONVERT:
            return _convert(conn, p, state, by, now)
    return ExecutionOutcome("unsupported", "That kind of request can't be run yet.")


# --- payouts ----------------------------------------------------------------------------------


def _matched(p: Proposal, state: FamilyState) -> TrustedPayee | None:
    exact, _ = rules.match_payee(p.payee, state.trusted_payees)
    return exact


def _narration(conn: sqlite3.Connection, p: Proposal) -> str:
    if p.kind is Kind.ALLOWANCE_RUN:
        return "Monthly allowance"
    if p.kind is Kind.SEND:
        return f"Transfer to {p.payee.name}"
    fee = None if p.fee_id is None else store.get_fee(conn, p.family_id, p.fee_id)
    return f"Fees {fee['invoice_ref']}" if fee is not None and fee["invoice_ref"] else "School fees"


def _start_payout(
    conn: sqlite3.Connection, provider_name: str, p: Proposal, state: FamilyState, by: str, now: datetime
) -> _PayoutJob:
    quote = p.quote
    # The rules passed, so this is the trusted payee. Its stored record is what the bank is sent, never the invoice's.
    target = _matched(p, state)
    payout_id = db.new_id("po")
    reference = f"karo-{p.id}"
    stamp = iso(now)
    conn.execute(
        "INSERT INTO payouts (id, proposal_id, family_id, provider, reference, amount_minor, currency, debit_minor, "
        "debit_currency, home_value_minor, kind, status, simulated, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
        (
            payout_id,
            p.id,
            p.family_id,
            provider_name,
            reference,
            p.amount.minor,
            p.amount.currency,
            quote.source.minor,
            quote.source.currency,
            rules.home_value_of(p, state).minor,
            p.kind.value,
            stamp,
            stamp,
        ),
    )
    ledger.post(
        conn,
        family_id=p.family_id,
        at=now,
        amount=Money(-quote.source.minor, quote.source.currency),
        kind="payout",
        ref=payout_id,
    )
    audit.record(
        conn,
        at=now,
        family_id=p.family_id,
        actor=by,
        event="payout.started",
        data={
            "payout_id": payout_id,
            "proposal_id": p.id,
            "reference": reference,
            "amount": str(p.amount),
            "debit": str(quote.source),
        },
    )
    return _PayoutJob(p, payout_id, reference, None, target.payee, target.payee.name, _narration(conn, p), now, True)


def _reconcile_job(conn: sqlite3.Connection, p: Proposal, state: FamilyState, earlier: sqlite3.Row) -> _PayoutJob:
    target = _matched(p, state)
    return _PayoutJob(
        p,
        earlier["id"],
        earlier["reference"],
        earlier["provider_id"],
        None if target is None else target.payee,
        p.payee.name if target is None else target.payee.name,
        _narration(conn, p),
        clock.parse(earlier["created_at"]),
        False,
        state.frozen,
    )


# --- step 3: the provider's answer ------------------------------------------------------------


def _settle(conn: sqlite3.Connection, job: _PayoutJob, result: PayoutResult, now: datetime) -> ExecutionOutcome:
    with db.transaction(conn):
        row = _payout_row(conn, job)
        # Another caller may have settled it while this one waited on the provider; then there is nothing to add.
        if row["status"] == "pending":
            _record(conn, job, result, now)
            row = _payout_row(conn, job)
        log_check = _log_check(conn)
    if row["status"] == "succeeded":
        receipt = _receipt(job, bool(row["simulated"]), log_check)
        return ExecutionOutcome("executed", receipt, job.payout_id)
    if row["status"] == "failed":
        return ExecutionOutcome("failed", _failure_text(job), job.payout_id)
    return _pending(job)


def _pending(job: _PayoutJob) -> ExecutionOutcome:
    p = job.proposal
    message = f"{ON_ITS_WAY}{p.amount} to {job.payee_name}. I'll message you when it lands."
    return ExecutionOutcome("pending", message, job.payout_id)


def _payout_row(conn: sqlite3.Connection, job: _PayoutJob) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM payouts WHERE id = ? AND family_id = ?", (job.payout_id, job.proposal.family_id)
    ).fetchone()


def _record(conn: sqlite3.Connection, job: _PayoutJob, result: PayoutResult, now: datetime) -> None:
    p = job.proposal
    conn.execute(
        "UPDATE payouts SET status = ?, provider_id = COALESCE(?, provider_id), simulated = ?, updated_at = ? "
        "WHERE id = ?",
        (result.status, result.provider_id, int(result.simulated), iso(now), job.payout_id),
    )
    data = {
        "payout_id": job.payout_id,
        "proposal_id": p.id,
        "reference": job.reference,
        "provider_id": result.provider_id,
        "status": result.status,
        "simulated": result.simulated,
    }
    main = store.main_payer(conn, p.family_id)
    if result.status == "failed":
        ledger.post(
            conn, family_id=p.family_id, at=now, amount=p.quote.source, kind="payout_reversal", ref=job.payout_id
        )
        store.update_proposal(conn, p.family_id, p.id, status="failed", now=now)
        audit.record(
            conn,
            at=now,
            family_id=p.family_id,
            actor="system",
            event="payout.failed",
            data={**data, "message": result.message},
        )
        outbox.notify(conn, main.id, _failure_text(job), now)
        return
    audit.record(conn, at=now, family_id=p.family_id, actor="system", event="payout.sent", data=data)
    if result.status == "succeeded":
        store.update_proposal(conn, p.family_id, p.id, status="executed", now=now)
        if p.kind is Kind.PAY_FEE and p.fee_id is not None:
            conn.execute("UPDATE fees SET status = 'paid' WHERE id = ? AND family_id = ?", (p.fee_id, p.family_id))
        outbox.notify(conn, main.id, _receipt(job, result.simulated, _log_check(conn)), now)


def _log_check(conn: sqlite3.Connection) -> str:
    """The start of the newest audit hash. Read in the transaction that writes the receipt, after the payout's row."""
    return conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()["hash"][:12]


def _receipt(job: _PayoutJob, simulated: bool, log_check: str) -> str:
    """One line. The reference and the provider's id stay in the payouts table, the log and the proof export."""
    p = job.proposal
    test_payment = " Test payment, simulated, no real money." if simulated else ""
    return f"Sent ✓ {p.amount} to {job.payee_name}. Cost {p.quote.source}.{test_payment} Log check {log_check}."


def _failure_text(job: _PayoutJob) -> str:
    p = job.proposal
    return f"The payment of {p.amount} to {job.payee_name} failed. Nothing was taken: the money is back in the balance."


# --- the kinds that stay inside the database --------------------------------------------------


def _executed(conn: sqlite3.Connection, p: Proposal, now: datetime, message: str) -> ExecutionOutcome:
    store.update_proposal(conn, p.family_id, p.id, status="executed", now=now)
    return ExecutionOutcome("executed", message)


def _ordinal(day: int) -> str:
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def done_text(p: Proposal, state: FamilyState) -> str:
    """What a proposal that ran has done, as one short line. The PIN page, the note in the chat and WhatsApp, and the
    page for a link used twice all say it the same way."""
    payee = p.payee.name if p.payee is not None else "the payee"
    match p.kind:
        case Kind.PAY_FEE | Kind.SEND | Kind.ALLOWANCE_RUN:
            return f"Sent {p.amount} to {payee}."
        case Kind.ALLOWANCE_SET:
            return f"Allowance set: {p.amount} on the {_ordinal(ALLOWANCE_DAY)} of each month."
        case Kind.SPLIT:
            names = ", ".join(state.member(member_id).name for member_id, _ in p.split)
            return f"Asked {names} for their shares of {p.amount}."
        case Kind.ADD_PAYEE:
            return f"{payee} is saved as a recipient."
        case Kind.UNFREEZE:
            return "Karo is unfrozen. Money can move again."
        case Kind.CONVERT:
            return f"Converted {p.quote.source} into {p.quote.target} (simulated)."
    return "That has been done."


# A line that opens with its own tick word ("Sent ✓", "Done ✓") needs no second one in front.
TICKED = re.compile(r"\w+ ✓")


def note_for(message: str) -> str:
    return message if TICKED.match(message) else f"Done ✓ {message}"


def _done(conn: sqlite3.Connection, p: Proposal, state: FamilyState, now: datetime) -> ExecutionOutcome:
    """For a kind with no receipt or payment link of its own: the main payer is told in the chat and on WhatsApp too,
    because the PIN page is the only other place the result shows and she may have left it."""
    message = done_text(p, state)
    outbox.notify(conn, state.main_payer.id, note_for(message), now)
    return _executed(conn, p, now, message)


def _split(
    conn: sqlite3.Connection, p: Proposal, state: FamilyState, by: str, now: datetime
) -> ExecutionOutcome | _SplitDone:
    fee = None if p.fee_id is None else store.get_fee(conn, p.family_id, p.fee_id)
    what = "the fees" if fee is None else f"the {fee['payee_name']} fees"
    asker = state.main_payer
    for member_id, amount in p.split:
        conn.execute(
            "INSERT INTO shares (id, family_id, fee_id, member_id, amount_minor, currency, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (db.new_id("sh"), p.family_id, p.fee_id, member_id, amount.minor, amount.currency, iso(now)),
        )
        relative = state.member(member_id)
        if relative.id == asker.id:
            notice = f"Your own share of {what} is {amount}. A payment link will follow."
        else:
            notice = f"{asker.name} has asked you to pay {amount} towards {what}. A payment link will follow."
        outbox.notify(conn, relative.id, notice, now)
    if p.fee_id is not None:
        conn.execute("UPDATE fees SET status = 'collecting' WHERE id = ? AND family_id = ?", (p.fee_id, p.family_id))
    audit.record(
        conn,
        at=now,
        family_id=p.family_id,
        actor=by,
        event="split.created",
        data={
            "proposal_id": p.id,
            "fee_id": p.fee_id,
            "shares": [{"member_id": member_id, "amount": str(amount)} for member_id, amount in p.split],
        },
    )
    outcome = _executed(conn, p, now, done_text(p, state))
    return outcome if p.fee_id is None else _SplitDone(outcome, p.fee_id)


def _allowance_set(
    conn: sqlite3.Connection, p: Proposal, state: FamilyState, by: str, now: datetime
) -> ExecutionOutcome:
    target = _matched(p, state)
    conn.execute("UPDATE standing_orders SET active = 0 WHERE family_id = ? AND active = 1", (p.family_id,))
    order_id = db.new_id("so")
    conn.execute(
        "INSERT INTO standing_orders (id, family_id, payee_id, amount_minor, currency, day_of_month, approved_by, "
        "proposal_id, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (order_id, p.family_id, target.id, p.amount.minor, p.amount.currency, ALLOWANCE_DAY, by, p.id),
    )
    audit.record(
        conn,
        at=now,
        family_id=p.family_id,
        actor=by,
        event="allowance.set",
        data={
            "proposal_id": p.id,
            "standing_order_id": order_id,
            "payee": target.payee.name,
            "amount": str(p.amount),
            "day_of_month": ALLOWANCE_DAY,
        },
    )
    return _done(conn, p, state, now)


def _add_payee(conn: sqlite3.Connection, p: Proposal, state: FamilyState, by: str, now: datetime) -> ExecutionOutcome:
    payee_id = db.new_id("tp")
    conn.execute(
        "INSERT INTO trusted_payees (id, family_id, name, details_json, trusted_since, source) "
        "VALUES (?, ?, ?, ?, ?, 'added')",
        (payee_id, p.family_id, p.payee.name, json.dumps(dict(p.payee.details), sort_keys=True), iso(now)),
    )
    audit.record(
        conn,
        at=now,
        family_id=p.family_id,
        actor=by,
        event="payee.added",
        data={"proposal_id": p.id, "payee_id": payee_id, "name": p.payee.name},
    )
    return _done(conn, p, state, now)


def _unfreeze(conn: sqlite3.Connection, p: Proposal, state: FamilyState, by: str, now: datetime) -> ExecutionOutcome:
    conn.execute("UPDATE families SET frozen = 0 WHERE id = ?", (p.family_id,))
    audit.record(conn, at=now, family_id=p.family_id, actor=by, event="family.unfrozen", data={"proposal_id": p.id})
    return _done(conn, p, state, now)


def _convert(conn: sqlite3.Connection, p: Proposal, state: FamilyState, by: str, now: datetime) -> ExecutionOutcome:
    quote = p.quote
    ledger.post(
        conn,
        family_id=p.family_id,
        at=now,
        amount=Money(-quote.source.minor, quote.source.currency),
        kind="conversion",
        ref=p.id,
    )
    ledger.post(conn, family_id=p.family_id, at=now, amount=quote.target, kind="conversion", ref=p.id)
    audit.record(
        conn,
        at=now,
        family_id=p.family_id,
        actor=by,
        event="conversion.done",
        data={"proposal_id": p.id, "from": str(quote.source), "to": str(quote.target), "simulated": True},
    )
    return _done(conn, p, state, now)
