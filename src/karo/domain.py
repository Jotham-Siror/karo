"""Plain records the rule checker reads. They hold data only; rules.py decides."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from enum import StrEnum

from karo.clock import iso
from karo.money import Money


class Role(StrEnum):
    MAIN_PAYER = "main_payer"
    RELATIVE = "relative"
    STUDENT = "student"


class Kind(StrEnum):
    SPLIT = "split"  # ask relatives for shares of a fee
    CONTRIBUTE = "contribute"  # a relative pays their own share in
    PAY_FEE = "pay_fee"  # pay an institution
    ALLOWANCE_SET = "allowance_set"  # create or change the allowance standing order
    ALLOWANCE_RUN = "allowance_run"  # one scheduled allowance payment
    CONVERT = "convert"  # move funds between the family's currencies
    SEND = "send"  # send money to a saved recipient abroad
    ADD_PAYEE = "add_payee"
    UNFREEZE = "unfreeze"


class Stage(StrEnum):
    PROPOSE = "propose"  # the agent has suggested it
    EXECUTE = "execute"  # about to call the provider


@dataclass(frozen=True)
class Member:
    id: str
    family_id: str
    role: Role
    name: str


@dataclass(frozen=True)
class Payee:
    """Where money goes, as an invoice claims it or as the trusted list stores it."""

    name: str
    details: tuple[tuple[str, str], ...]  # sorted (field, value) pairs, e.g. sort_code, account_number

    @classmethod
    def of(cls, name: str, **details: str) -> Payee:
        return cls(name, tuple(sorted(details.items())))


@dataclass(frozen=True)
class TrustedPayee:
    id: str
    family_id: str
    payee: Payee
    trusted_since: datetime


@dataclass(frozen=True)
class Quote:
    """A provider quote: the family pays `source`, the payee receives `target`."""

    id: str
    family_id: str
    source: Money
    target: Money
    expires_at: datetime


@dataclass(frozen=True)
class Share:
    id: str
    family_id: str
    member_id: str
    amount: Money
    paid: Money

    @property
    def remaining(self) -> Money:
        return self.amount - self.paid


@dataclass(frozen=True)
class StandingOrder:
    id: str
    family_id: str
    payee: Payee
    amount: Money  # what the student receives each run
    day_of_month: int
    approved_by: str | None  # member id whose PIN approved it
    active: bool


@dataclass(frozen=True)
class Confirmation:
    """Written only by the approvals module after a correct PIN."""

    proposal_id: str
    member_id: str
    digest: str  # Proposal.digest() of what the PIN approved
    at: datetime


@dataclass(frozen=True)
class Policy:
    """A family's limits, all in its home currency. Settings, not code."""

    home_currency: str
    payment_cap: Money
    allowance_monthly_cap: Money
    daily_cap: Money
    new_payee_threshold: Money
    new_payee_wait: timedelta

    def __post_init__(self) -> None:
        for name in ("payment_cap", "allowance_monthly_cap", "daily_cap", "new_payee_threshold"):
            if getattr(self, name).currency != self.home_currency:
                raise ValueError(f"{name} must be in the home currency {self.home_currency}")


@dataclass(frozen=True)
class FamilyState:
    """Everything the checker needs about one family, read in one transaction."""

    family_id: str
    policy: Policy
    members: tuple[Member, ...]
    trusted_payees: tuple[TrustedPayee, ...] = ()
    shares: tuple[Share, ...] = ()
    standing_orders: tuple[StandingOrder, ...] = ()
    balances: tuple[Money, ...] = ()
    frozen: bool = False
    pin_locked_until: datetime | None = None
    outflow_today: Money | None = None  # home currency, family's local day
    allowance_this_month: Money | None = None  # home currency

    def member(self, member_id: str) -> Member | None:
        return next((m for m in self.members if m.id == member_id), None)

    @property
    def main_payer(self) -> Member | None:
        return next((m for m in self.members if m.role is Role.MAIN_PAYER), None)

    def share(self, share_id: str | None) -> Share | None:
        return next((s for s in self.shares if s.id == share_id), None)

    def standing_order(self, order_id: str | None) -> StandingOrder | None:
        return next((o for o in self.standing_orders if o.id == order_id), None)

    def balance(self, currency: str) -> Money:
        return next((b for b in self.balances if b.currency == currency), Money(0, currency))


def _canonical(value: object) -> object:
    """JSON-ready form of a Proposal field. A type it doesn't know raises, so no field is skipped silently."""
    match value:
        case None | str() | int():
            return value
        case Money():
            return [value.minor, value.currency]
        case Payee():
            return [value.name, _canonical(value.details)]
        case Quote():
            return [value.id, value.family_id, _canonical(value.source), _canonical(value.target), iso(value.expires_at)]
        case tuple():
            return [_canonical(item) for item in value]
    raise TypeError(f"can't put {type(value).__name__} in a proposal digest")


@dataclass(frozen=True)
class Proposal:
    """Something the agent wants to happen. Built by code from validated tool input."""

    id: str
    family_id: str
    kind: Kind
    requested_by: str
    amount: Money | None = None
    payee: Payee | None = None
    quote: Quote | None = None
    share_id: str | None = None
    standing_order_id: str | None = None
    fee_id: str | None = None  # the fee this pays or splits
    split: tuple[tuple[str, Money], ...] = field(default=())  # (member id, amount) for SPLIT

    def __post_init__(self) -> None:
        if self.amount is not None and self.amount.minor <= 0:
            raise ValueError("amount must be positive")

    def digest(self) -> str:
        """Fingerprint of every field. The PIN approves this value, so any later edit voids the approval."""
        content = {f.name: _canonical(getattr(self, f.name)) for f in fields(self)}
        text = json.dumps(content, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class CheckContext:
    now: datetime
    stage: Stage
    actor: Member | None  # the person acting now; None only for the allowance scheduler
    confirmation: Confirmation | None = None
