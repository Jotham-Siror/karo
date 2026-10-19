"""A made-up demo family: a mother in Nairobi, her brother in Kenya, her sister in Uganda,
and her son at a UK university. Caps are in KES at roughly 165 KES to the pound."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import respx

from karo.domain import (
    CheckContext,
    Confirmation,
    FamilyState,
    Kind,
    Member,
    Payee,
    Policy,
    Proposal,
    Quote,
    Role,
    Share,
    Stage,
    StandingOrder,
    TrustedPayee,
)
from karo.money import Money

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam-otieno"

MUM = Member("m-amina", FAM, Role.MAIN_PAYER, "Amina")
UNCLE = Member("m-baraka", FAM, Role.RELATIVE, "Baraka")
AUNT = Member("m-nakato", FAM, Role.RELATIVE, "Nakato")
SON = Member("m-jabali", FAM, Role.STUDENT, "Jabali")
STRANGER = Member("m-x", "fam-other", Role.MAIN_PAYER, "Other family's payer")

UNIVERSITY = Payee.of(
    "University of Westbridge", country="GB", currency="GBP", sort_code="20-40-60", account_number="41234567"
)
SON_ACCOUNT = Payee.of("Jabali Otieno", country="GB", currency="GBP", sort_code="30-90-11", account_number="70012345")


EXCHANGE_URL = "https://open.er-api.com/v6/latest/{source}"


def rates_payload(**rates: float) -> dict:
    return {"result": "success", "rates": rates}


@pytest.fixture
def exchange_api():
    """The exchange-rate API, mocked. A test that reaches karo.rates must use this: the suite never uses the network."""
    with respx.mock(assert_all_called=False) as router:
        yield router


def kes(amount: str) -> Money:
    return Money.parse(amount, "KES")


def gbp(amount: str) -> Money:
    return Money.parse(amount, "GBP")


def quote(source: Money, target: Money, *, expires_in=timedelta(minutes=15), family_id=FAM, qid="q-1") -> Quote:
    return Quote(qid, family_id, source, target, NOW + expires_in)


@pytest.fixture
def state() -> FamilyState:
    return FamilyState(
        family_id=FAM,
        policy=Policy(
            home_currency="KES",
            payment_cap=kes("2500000"),
            allowance_monthly_cap=kes("165000"),
            daily_cap=kes("3300000"),
            new_payee_threshold=kes("33000"),
            new_payee_wait=timedelta(hours=24),
        ),
        members=(MUM, UNCLE, AUNT, SON),
        trusted_payees=(
            TrustedPayee("tp-uni", FAM, UNIVERSITY, NOW - timedelta(days=30)),
            TrustedPayee("tp-son", FAM, SON_ACCOUNT, NOW - timedelta(days=30)),
        ),
        shares=(
            Share("sh-baraka", FAM, UNCLE.id, gbp("2000"), gbp("0")),
            Share("sh-nakato", FAM, AUNT.id, gbp("2000"), gbp("0")),
        ),
        standing_orders=(StandingOrder("so-1", FAM, SON_ACCOUNT, gbp("500"), 1, MUM.id, True),),
        balances=(kes("1000000"),),
        outflow_today=kes("0"),
        allowance_this_month=kes("0"),
    )


def fee_proposal(**changes) -> Proposal:
    base = Proposal(
        id="p-fee",
        family_id=FAM,
        kind=Kind.PAY_FEE,
        requested_by=MUM.id,
        amount=gbp("6000"),
        payee=UNIVERSITY,
        quote=quote(kes("990000"), gbp("6000")),
    )
    return replace(base, **changes)


def allowance_run(**changes) -> Proposal:
    base = Proposal(
        id="p-allow",
        family_id=FAM,
        kind=Kind.ALLOWANCE_RUN,
        requested_by="scheduler",
        amount=gbp("500"),
        payee=SON_ACCOUNT,
        quote=quote(kes("82500"), gbp("500")),
        standing_order_id="so-1",
    )
    return replace(base, **changes)


def propose(actor: Member | None = MUM, now: datetime = NOW) -> CheckContext:
    return CheckContext(now=now, stage=Stage.PROPOSE, actor=actor)


def execute(proposal: Proposal, actor: Member | None = MUM, now: datetime = NOW, confirmed_by: Member | None = MUM):
    confirmation = Confirmation(proposal.id, confirmed_by.id, proposal.digest(), now) if confirmed_by else None
    return CheckContext(now=now, stage=Stage.EXECUTE, actor=actor, confirmation=confirmation)
