"""Turns what the agent suggests into a stored proposal: runs the rules, holds it for the PIN, tells the main payer.

The confirm link goes to the main payer's outbox and nowhere else. The outcome the agent gets back never carries it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from karo import approvals, audit, db, outbox, rules, store
from karo.domain import CheckContext, FamilyState, Kind, Member, Payee, Proposal, Stage
from karo.money import Money
from karo.rules import Decision, Outcome, RuleResult

ProposalStatus = Literal["blocked", "waiting", "awaiting_pin", "confirmed"]


@dataclass(frozen=True)
class ProposalOutcome:
    proposal_id: str
    status: ProposalStatus
    decision: Decision
    message: str  # a plain sentence for the agent to relay; never the confirm link


def when_text(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%a %d %b, %H:%M %Z")


def block_reasons(decision: Decision) -> str:
    return " ".join(r.reason for r in decision.results if r.outcome is Outcome.BLOCK)


def audit_decision(
    conn: sqlite3.Connection, *, p: Proposal, stage: Stage, decision: Decision, actor: str, now: datetime
) -> None:
    audit.record(
        conn,
        at=now,
        family_id=p.family_id,
        actor=actor,
        event="rules.checked",
        data={
            "proposal_id": p.id,
            "stage": stage.value,
            "verdict": decision.verdict.value,
            # A list, not a dict keyed by rule: the audit log refuses keys that look secret, and "pin_lockout" does.
            "results": [{"rule": r.rule, "outcome": r.outcome.value, "reason": r.reason} for r in decision.results],
        },
    )


def fee_blocks(conn: sqlite3.Connection, p: Proposal, state: FamilyState) -> tuple[RuleResult, ...]:
    """Fee checks the rule checker can't make: they need the fees table, which is not part of the family's state."""
    if p.fee_id is None:
        # Without a fee, none of the checks below can run, so a split or a payment must not go ahead without one.
        if p.kind in (Kind.SPLIT, Kind.PAY_FEE):
            return (_fee_block("This needs an invoice recorded first."),)
        return ()
    fee = store.get_fee(conn, p.family_id, p.fee_id)
    if fee is None:
        return (_fee_block("That fee wasn't found."),)
    if fee["status"] == "paid":
        return (_fee_block("That fee has already been paid."),)
    fee_amount = Money(fee["amount_minor"], fee["currency"])
    if p.kind is Kind.SPLIT:
        # The split rule makes the shares add up to p.amount, so p.amount being the whole fee makes them add up to it.
        if p.amount != fee_amount:
            return (_fee_block(f"The split has to cover the whole fee of {fee_amount}."),)
        if fee["status"] != "open" or store.shares_for_fee(conn, p.family_id, p.fee_id):
            return (_fee_block("Shares have already been asked for on that fee."),)
    if p.kind is Kind.PAY_FEE:
        if p.amount != fee_amount:
            return (_fee_block(f"The payment has to be the fee's full amount of {fee_amount}."),)
        if not _same_trusted_payee(
            p.payee, Payee.of(fee["payee_name"], **json.loads(fee["payee_details_json"])), state
        ):
            return (_fee_block("This payment's payee doesn't match the invoice for this fee."),)
        if _payment_under_way(conn, p):
            return (_fee_block("A payment for that fee is already under way."),)
    return ()


def _same_trusted_payee(claim: Payee | None, invoice: Payee, state: FamilyState) -> bool:
    """Both resolve to one trusted payee. With no payee at all the trusted_payee rule already blocks."""
    if claim is None:
        return True
    paying, _ = rules.match_payee(claim, state.trusted_payees)
    owed, _ = rules.match_payee(invoice, state.trusted_payees)
    return paying is not None and owed is not None and paying.id == owed.id


def _fee_block(reason: str) -> RuleResult:
    return RuleResult("fee", Outcome.BLOCK, reason)


def _payment_under_way(conn: sqlite3.Connection, p: Proposal) -> bool:
    """True if another proposal's payout for this fee is pending or done: the fee only turns paid once it lands."""
    row = conn.execute(
        "SELECT 1 FROM payouts o JOIN proposals q ON q.id = o.proposal_id "
        "WHERE q.family_id = ? AND o.status != 'failed' AND o.proposal_id != ? "
        "AND json_extract(q.payload_json, '$.fee_id') = ?",
        (p.family_id, p.id, p.fee_id),
    ).fetchone()
    return row is not None


def _name(state: FamilyState, member_id: str) -> str:
    found = state.member(member_id)
    return found.name if found is not None else "someone"


def summary(p: Proposal, state: FamilyState) -> str:
    """One plain line saying what a proposal does. The outbox and the PIN page both show it."""
    payee = p.payee.name if p.payee is not None else "the payee"
    cost = f" (costs {p.quote.source})" if p.quote is not None else ""
    match p.kind:
        case Kind.PAY_FEE:
            return f"Pay {p.amount} to {payee}{cost}"
        case Kind.ALLOWANCE_RUN:
            return f"Send the monthly allowance of {p.amount} to {payee}{cost}"
        case Kind.ALLOWANCE_SET:
            return f"Set the monthly allowance to {p.amount} for {payee}"
        case Kind.SPLIT:
            shares = ", ".join(f"{_name(state, member_id)} {amount}" for member_id, amount in p.split)
            return f"Ask the family to share {p.amount}: {shares}"
        case Kind.CONTRIBUTE:
            return f"Pay {p.amount} towards your share"
        case Kind.CONVERT:
            if p.quote is None:
                return f"Convert money to get {p.amount}"
            return f"Convert {p.quote.source} into {p.quote.target}"
        case Kind.ADD_PAYEE:
            # Every detail, because the PIN approves exactly this and the account is what makes a payee trusted.
            pairs = p.payee.details if p.payee is not None else ()
            details = ", ".join(f"{field.replace('_', ' ')} {value}" for field, value in pairs)
            return f"Add {payee} to the trusted payees" + (f" ({details})" if details else "")
        case Kind.UNFREEZE:
            return "Unfreeze Karo so money can move again"


def _lookup_blocks(
    conn: sqlite3.Connection, p: Proposal, state: FamilyState, quote_id: str | None
) -> tuple[RuleResult, ...]:
    blocks = []
    if quote_id is not None and p.quote is None:
        blocks.append(RuleResult("quote_lookup", Outcome.BLOCK, "That quote wasn't found."))
    if p.kind is Kind.ADD_PAYEE and p.payee is None:
        blocks.append(RuleResult("payee", Outcome.BLOCK, "There is no payee to add."))
    return tuple(blocks) + fee_blocks(conn, p, state)


def _status_for(kind: Kind, verdict: Outcome) -> ProposalStatus:
    if verdict is Outcome.BLOCK:
        return "blocked"
    if kind in rules.PIN_KINDS:
        return "awaiting_pin"  # a WAIT still needs the PIN now; the wait is enforced again when it runs
    return "confirmed" if verdict is Outcome.PASS else "waiting"


def _outcome_message(status: ProposalStatus, decision: Decision, main: Member, when: str) -> str:
    match status:
        case "blocked":
            return f"That isn't allowed. {block_reasons(decision)}"
        case "awaiting_pin":
            later = f" It can run after {when}." if decision.until is not None else ""
            return (
                f"I've sent {main.name} a link to confirm this with a PIN. Nothing moves until it's confirmed.{later}"
            )
        case "waiting":
            return f"That has to wait. It can run after {when}."
        case "confirmed":
            return "That's approved and ready to run."


def propose(
    conn: sqlite3.Connection,
    *,
    family_id: str,
    actor: Member,
    kind: Kind,
    now: datetime,
    base_url: str,
    amount: Money | None = None,
    payee: Payee | None = None,
    quote_id: str | None = None,
    share_id: str | None = None,
    standing_order_id: str | None = None,
    fee_id: str | None = None,
    split: Iterable[tuple[str, Money]] = (),
) -> ProposalOutcome:
    """Records a proposal and the rules' verdict on it. Moves no money: only the executor does."""
    with db.transaction(conn):
        quote = None if quote_id is None else store.get_quote(conn, family_id, quote_id)
        p = Proposal(
            id=db.new_id("prop"),
            family_id=family_id,
            kind=kind,
            requested_by=actor.id,
            amount=amount,
            payee=payee,
            quote=quote,
            share_id=share_id,
            standing_order_id=standing_order_id,
            fee_id=fee_id,
            split=tuple(split),
        )
        state = store.load_state(conn, family_id, now)
        checked = rules.check(p, state, CheckContext(now, Stage.PROPOSE, actor))
        decision = Decision(_lookup_blocks(conn, p, state, quote_id) + checked.results)
        status = _status_for(kind, decision.verdict)

        config = store.family_config(conn, family_id)
        main = store.main_payer(conn, family_id)
        when = "" if decision.until is None else when_text(decision.until, config.timezone)

        store.insert_proposal(conn, p, status=status, decision=decision, now=now)
        audit.record(
            conn,
            at=now,
            family_id=family_id,
            actor=actor.id,
            event="proposal.created",
            data={"proposal_id": p.id, "kind": kind.value, "requested_by": actor.id, "digest": p.digest()},
        )
        audit_decision(conn, p=p, stage=Stage.PROPOSE, decision=decision, actor=actor.id, now=now)

        if status == "awaiting_pin":
            url = approvals.create_link(
                conn,
                family_id=family_id,
                proposal_id=p.id,
                member_id=main.id,
                now=now,
                base_url=base_url,
                ttl=config.quote_ttl,
            )
            later = f". It can run after {when}." if decision.until is not None else ""
            outbox.notify(conn, main.id, f"{summary(p, state)}{later}\nConfirm with your PIN: {url}", now)

        for warning in decision.warnings:
            outbox.notify(conn, main.id, f"Warning: {warning.reason}", now)
            audit.record(
                conn,
                at=now,
                family_id=family_id,
                actor=actor.id,
                event="warning.raised",
                data={"proposal_id": p.id, "rule": warning.rule, "reason": warning.reason},
            )
    return ProposalOutcome(p.id, status, decision, _outcome_message(status, decision, main, when))


def freeze(conn: sqlite3.Connection, *, family_id: str, actor: Member, now: datetime) -> str:
    """Any member of the family can stop all money movement. Only the main payer can undo it, with the PIN."""
    with db.transaction(conn):
        who = store.member(conn, family_id, actor.id)
        if who is None:
            return "I can't freeze this family for you: you aren't one of its members."
        if store.family_row(conn, family_id)["frozen"]:
            return "Karo is already frozen. The main payer can unfreeze it with the PIN."
        conn.execute("UPDATE families SET frozen = 1 WHERE id = ?", (family_id,))
        audit.record(conn, at=now, family_id=family_id, actor=who.id, event="family.frozen", data={"member_id": who.id})
        main = store.main_payer(conn, family_id)
        outbox.notify(
            conn, main.id, f"{who.name} froze Karo. No money will move until you unfreeze it with your PIN.", now
        )
    return "Karo is frozen. No money will move until the main payer unfreezes it with the PIN."
