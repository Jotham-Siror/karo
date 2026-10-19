"""The rule checker: ordinary code that decides whether a proposal may happen.

The agent can only propose. Every proposal is checked here when it is made and again just
before the provider is called. Every applicable rule runs and reports, so the log records all
reasons, not just the first. Nothing here reads free text from invoices or chat: a prompt
trick can change what the agent proposes, never what this module allows.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from karo.domain import CheckContext, FamilyState, Kind, Payee, Proposal, Role, Stage, TrustedPayee
from karo.money import Money


class Outcome(StrEnum):
    PASS = "pass"
    WAIT = "wait"  # allowed later, at `until`
    BLOCK = "block"


@dataclass(frozen=True)
class RuleResult:
    rule: str
    outcome: Outcome
    reason: str
    until: datetime | None = None
    warn: bool = False  # the main payer must be told, e.g. changed bank details


@dataclass(frozen=True)
class Decision:
    results: tuple[RuleResult, ...]

    @property
    def verdict(self) -> Outcome:
        outcomes = {r.outcome for r in self.results}
        if Outcome.BLOCK in outcomes:
            return Outcome.BLOCK
        if Outcome.WAIT in outcomes:
            return Outcome.WAIT
        return Outcome.PASS

    @property
    def allowed(self) -> bool:
        return self.verdict is Outcome.PASS

    @property
    def until(self) -> datetime | None:
        waits = [r.until for r in self.results if r.outcome is Outcome.WAIT and r.until]
        return max(waits) if waits else None

    @property
    def warnings(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.results if r.warn)


# Which kinds each rule applies to.
PIN_KINDS = frozenset({Kind.SPLIT, Kind.PAY_FEE, Kind.ALLOWANCE_SET, Kind.CONVERT, Kind.ADD_PAYEE, Kind.UNFREEZE})
OUTBOUND = frozenset({Kind.PAY_FEE, Kind.ALLOWANCE_RUN})  # money leaving the family
TO_PAYEE = OUTBOUND | {Kind.ALLOWANCE_SET}
NEEDS_QUOTE = OUTBOUND | {Kind.CONVERT}
VALUED = OUTBOUND | {Kind.ALLOWANCE_SET}

Rule = Callable[[Proposal, FamilyState, CheckContext], RuleResult | None]


def _pass(rule: str, reason: str = "ok") -> RuleResult:
    return RuleResult(rule, Outcome.PASS, reason)


def _block(rule: str, reason: str, **kw) -> RuleResult:
    return RuleResult(rule, Outcome.BLOCK, reason, **kw)


def _home_value(p: Proposal, s: FamilyState) -> Money | None:
    """What the proposal is worth in the family's home currency, from its own amount or its quote."""
    home = s.policy.home_currency
    if p.amount is not None and p.amount.currency == home:
        return p.amount
    if p.quote is not None and p.quote.source.currency == home:
        return p.quote.source
    return None


def same_family(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult:
    foreign = []
    if p.family_id != s.family_id:
        foreign.append("proposal")
    if c.actor is not None and c.actor.family_id != s.family_id:
        foreign.append("person")
    if p.quote is not None and p.quote.family_id != s.family_id:
        foreign.append("quote")
    if foreign:
        return _block("same_family", f"Refused: the {', '.join(foreign)} belongs to another family.")
    return _pass("same_family")


def role(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult:
    actor = c.actor
    if p.kind is Kind.ALLOWANCE_RUN:
        order = s.standing_order(p.standing_order_id)
        if order is None or not order.active:
            return _block("role", "There is no active allowance standing order for this run.")
        if actor is not None and actor.role is not Role.MAIN_PAYER:
            return _block("role", "Only the main payer or the scheduler can run the allowance.")
        return _pass("role")
    if actor is None:
        return _block("role", "Only scheduled allowance runs can happen without a person.")
    if s.member(actor.id) is None:
        return _block("role", "This person isn't a member of the family.")
    if p.kind is Kind.CONTRIBUTE:
        share = s.share(p.share_id)
        if share is None:
            return _block("role", "There is no such share.")
        if share.member_id != actor.id:
            return _block("role", "Relatives can only pay their own share.")
        return _pass("role")
    if actor.role is not Role.MAIN_PAYER:
        return _block("role", f"Only the main payer can do this ({p.kind}).")
    return _pass("role")


def freeze(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult:
    if p.kind is Kind.UNFREEZE:
        return _pass("freeze") if s.frozen else _block("freeze", "Karo isn't frozen.")
    if s.frozen:
        return _block("freeze", "All money movement is frozen. The main payer can unfreeze with the PIN.")
    return _pass("freeze")


def pin_lockout(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult | None:
    if p.kind not in PIN_KINDS:
        return None
    if s.pin_locked_until is not None and c.now < s.pin_locked_until:
        return _block("pin_lockout", "Locked after repeated wrong PINs.", until=s.pin_locked_until)
    return _pass("pin_lockout")


def home_value(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult | None:
    if p.kind not in VALUED:
        return None
    if _home_value(p, s) is None:
        return _block("home_value", f"Can't value this in {s.policy.home_currency}, so the limits can't be checked.")
    return _pass("home_value")


def payment_cap(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult | None:
    value = _home_value(p, s)
    if p.kind not in OUTBOUND or value is None:
        return None
    cap = s.policy.payment_cap
    if value > cap:
        return _block("payment_cap", f"{value} is over the per-payment limit of {cap}.")
    return _pass("payment_cap")


def allowance_cap(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult | None:
    value = _home_value(p, s)
    if p.kind not in {Kind.ALLOWANCE_SET, Kind.ALLOWANCE_RUN} or value is None:
        return None
    cap = s.policy.allowance_monthly_cap
    total = value
    if p.kind is Kind.ALLOWANCE_RUN:
        total = value + (s.allowance_this_month or Money(0, cap.currency))
    if total > cap:
        return _block("allowance_cap", f"{total} this month would be over the allowance limit of {cap}.")
    return _pass("allowance_cap")


def daily_cap(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult | None:
    value = _home_value(p, s)
    if p.kind not in OUTBOUND or value is None:
        return None
    cap = s.policy.daily_cap
    total = value + (s.outflow_today or Money(0, cap.currency))
    if total > cap:
        return _block("daily_cap", f"{total} leaving today would be over the daily limit of {cap}.")
    return _pass("daily_cap")


_NAME_JUNK = re.compile(r"[^0-9a-z]+")
_VALUE_JUNK = re.compile(r"[\s\-]+")


def _norm_name(name: str) -> str:
    return _NAME_JUNK.sub(" ", name.casefold()).strip()


def _norm_details(payee: Payee) -> dict[str, str]:
    return {k.casefold(): _VALUE_JUNK.sub("", v).upper() for k, v in payee.details}


def _match(claim: Payee, trusted: tuple[TrustedPayee, ...]) -> tuple[TrustedPayee | None, TrustedPayee | None]:
    """Return (exact match on details, trusted payee with the same name but other details)."""
    details = _norm_details(claim)
    exact = next((t for t in trusted if _norm_details(t.payee) == details), None)
    if exact is not None:
        return exact, None
    namesake = next((t for t in trusted if _norm_name(t.payee.name) == _norm_name(claim.name)), None)
    return None, namesake


def trusted_payee(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult | None:
    if p.kind not in TO_PAYEE:
        return None
    if p.payee is None:
        return _block("trusted_payee", "No payee given.")
    exact, namesake = _match(p.payee, s.trusted_payees)
    if exact is not None:
        return _pass("trusted_payee", f"Matches trusted payee {exact.payee.name}.")
    if namesake is not None:
        claimed, stored = _norm_details(p.payee), _norm_details(namesake.payee)
        changed = sorted(k for k in claimed.keys() | stored.keys() if claimed.get(k) != stored.get(k))
        fields = ", ".join(k.replace("_", " ") for k in changed)
        return _block(
            "trusted_payee",
            f"The details for {namesake.payee.name} don't match the trusted details (changed: {fields}). "
            "This could be a fake invoice. Nothing was paid.",
            warn=True,
        )
    return _block("trusted_payee", f"{p.payee.name} is not on the family's trusted payee list.")


def new_payee_wait(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult | None:
    value = _home_value(p, s)
    if p.kind not in OUTBOUND or p.payee is None or value is None:
        return None
    exact, _ = _match(p.payee, s.trusted_payees)
    if exact is None:
        return None  # trusted_payee already blocks it
    ready_at = exact.trusted_since + s.policy.new_payee_wait
    if c.now < ready_at and value > s.policy.new_payee_threshold:
        return RuleResult("new_payee_wait", Outcome.WAIT, f"{exact.payee.name} is a new payee.", until=ready_at)
    return _pass("new_payee_wait")


def quote(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult | None:
    if p.kind not in NEEDS_QUOTE:
        return None
    q = p.quote
    if q is None:
        return _block("quote", "There is no quote for this payment.")
    if c.now >= q.expires_at:
        return _block("quote", "The quote has expired. Get a new one.")
    if p.amount != q.target:
        return _block("quote", f"The quote is for {q.target}, not {p.amount}.")
    return _pass("quote")


def funds(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult | None:
    if c.stage is not Stage.EXECUTE or p.kind not in NEEDS_QUOTE or p.quote is None:
        return None
    need, have = p.quote.source, s.balance(p.quote.source.currency)
    if have < need:
        return _block("funds", f"Not enough funds: {have} held, {need} needed.")
    return _pass("funds")


def split(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult | None:
    if p.kind is not Kind.SPLIT:
        return None
    if p.amount is None or not p.split:
        return _block("split", "A split needs a total and at least one share.")
    member_ids = [m for m, _ in p.split]
    if len(set(member_ids)) != len(member_ids):
        return _block("split", "Each person can have only one share.")
    for member_id, amount in p.split:
        member = s.member(member_id)
        if member is None:
            return _block("split", "A share is for someone outside the family.")
        if member.role is Role.STUDENT:
            return _block("split", "The student can't be given a share.")
        if amount.currency != p.amount.currency or amount.minor <= 0:
            return _block("split", f"Each share must be a positive amount in {p.amount.currency}.")
    total = sum((a for _, a in p.split), Money(0, p.amount.currency))
    if total != p.amount:
        return _block("split", f"The shares add up to {total}, not {p.amount}.")
    return _pass("split")


def share_amount(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult | None:
    if p.kind is not Kind.CONTRIBUTE:
        return None
    share = s.share(p.share_id)
    if share is None or p.amount is None:
        return _block("share_amount", "A contribution needs a share and an amount.")
    if p.amount.currency != share.amount.currency or p.amount > share.remaining:
        return _block("share_amount", f"Only {share.remaining} is left to pay on this share.")
    return _pass("share_amount")


def confirmation(p: Proposal, s: FamilyState, c: CheckContext) -> RuleResult | None:
    if c.stage is not Stage.EXECUTE:
        return None
    main = s.main_payer
    if p.kind is Kind.ALLOWANCE_RUN:
        order = s.standing_order(p.standing_order_id)
        if order is None or main is None or order.approved_by != main.id:
            return _block("confirmation", "The standing order wasn't approved with the main payer's PIN.")
        if p.amount != order.amount or p.payee != order.payee:
            return _block("confirmation", "This run doesn't match the approved standing order.")
        return _pass("confirmation", "Covered by the approved standing order.")
    if p.kind not in PIN_KINDS:
        return None
    conf = c.confirmation
    if conf is None or main is None or conf.proposal_id != p.id or conf.member_id != main.id:
        return _block("confirmation", "Needs the main payer's PIN confirmation for this exact proposal.")
    return _pass("confirmation")


RULES: tuple[Rule, ...] = (
    same_family,
    role,
    freeze,
    pin_lockout,
    home_value,
    payment_cap,
    allowance_cap,
    daily_cap,
    trusted_payee,
    new_payee_wait,
    quote,
    funds,
    split,
    share_amount,
    confirmation,
)


def check(proposal: Proposal, state: FamilyState, ctx: CheckContext) -> Decision:
    results = (rule(proposal, state, ctx) for rule in RULES)
    return Decision(tuple(r for r in results if r is not None))
