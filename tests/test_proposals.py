"""Quotes, proposals, summaries and freezing. Every test names the bug it would catch."""

import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from karo import audit, clock, db, ledger, proposals, quotes, seed, store
from karo.domain import Kind, Payee, Proposal
from karo.money import Money
from karo.providers.standin import StandInProvider
from karo.rules import Outcome

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam_otieno"
OTHER = "fam_mwangi"
PIN = "4821"
PEPPER = "test-pepper"
BASE_URL = "https://karo.example"
RATES = {("KES", "GBP"): Decimal("0.00578"), ("GBP", "KES"): Decimal("173")}


def kes(amount: str) -> Money:
    return Money.parse(amount, "KES")


def gbp(amount: str) -> Money:
    return Money.parse(amount, "GBP")


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    # Seeding hashes two PINs with Argon2, which is deliberately slow: do it once and copy the file per test.
    path = tmp_path_factory.mktemp("template") / "karo.db"
    connection = db.connect(path)
    db.init(connection)
    seed.seed_demo(connection, pin=PIN, pepper=PEPPER, now=NOW)
    connection.close()
    return path


@pytest.fixture
def conn(seeded_db, tmp_path):
    target = tmp_path / "karo.db"
    shutil.copyfile(seeded_db, target)
    connection = db.connect(target)
    yield connection
    connection.close()


@pytest.fixture
def provider() -> StandInProvider:
    return StandInProvider(rates=RATES, secret="standin-test-secret", base_url=BASE_URL)


def member(conn, member_id: str, family_id: str = FAM):
    return store.member(conn, family_id, member_id)


def westbridge(conn) -> Payee:
    state = store.load_state(conn, FAM, NOW)
    return next(t.payee for t in state.trusted_payees if t.id == "tp_westbridge")


def add_trusted_payee(conn, payee_id: str, name: str, details: dict, since: datetime) -> Payee:
    conn.execute(
        "INSERT INTO trusted_payees (id, family_id, name, details_json, trusted_since, source) "
        "VALUES (?, ?, ?, ?, ?, 'added')",
        (payee_id, FAM, name, json.dumps(details), clock.iso(since)),
    )
    return Payee.of(name, **details)


def new_fee(conn, amount: str = "6000.00") -> str:
    return store.insert_fee(
        conn, family_id=FAM, payee=westbridge(conn), amount=gbp(amount), due_date=None, invoice_ref="INV-77", now=NOW
    )


def propose_fee_payment(conn, provider, *, payee=None, fee_id=None, quote_id=None, actor="m_amina", target="6000.00"):
    quote = quotes.create_quote(conn, provider, family_id=FAM, source_currency="KES", target=gbp(target), now=NOW)
    return proposals.propose(
        conn,
        family_id=FAM,
        actor=member(conn, actor),
        kind=Kind.PAY_FEE,
        now=NOW,
        base_url=BASE_URL,
        amount=quote.target,
        payee=payee or westbridge(conn),
        quote_id=quote_id or quote.id,
        fee_id=fee_id,
    )


def texts(conn, member_id: str) -> list[str]:
    rows = conn.execute("SELECT text FROM outbox WHERE member_id = ? ORDER BY id", (member_id,))
    return [r["text"] for r in rows]


def events(conn, name: str, family_id: str = FAM) -> list[dict]:
    return [e for e in audit.events(conn, family_id) if e["event"] == name]


def proposal_row(conn, proposal_id: str):
    return store.get_proposal(conn, FAM, proposal_id)[1]


# --- quotes ----------------------------------------------------------------------------------


def test_create_quote_saves_a_quote_that_expires_after_the_family_ttl(conn, provider):
    # Bug caught: a quote with no expiry, or one that ignores the family's quote_ttl_seconds.
    quote = quotes.create_quote(conn, provider, family_id=FAM, source_currency="KES", target=gbp("6000.00"), now=NOW)
    assert quote.source == kes("1038062.29")
    assert quote.target == gbp("6000.00")
    assert quote.expires_at == NOW + timedelta(seconds=900)
    assert store.get_quote(conn, FAM, quote.id) == quote


def test_a_quote_is_invisible_to_another_family(conn, provider):
    # Bug caught: a quote saved without its family could be reused by someone else's proposal.
    quote = quotes.create_quote(conn, provider, family_id=FAM, source_currency="KES", target=gbp("10.00"), now=NOW)
    assert store.get_quote(conn, OTHER, quote.id) is None


def test_create_quote_is_audited_without_secrets(conn, provider):
    # Bug caught: a quote the family was shown that leaves no trace in the log.
    quote = quotes.create_quote(conn, provider, family_id=FAM, source_currency="KES", target=gbp("6000.00"), now=NOW)
    (event,) = events(conn, "quote.created")
    assert event["data"]["quote_id"] == quote.id
    assert event["data"]["source"] == "KES 1,038,062.29"


# --- propose: the PIN path ------------------------------------------------------------------


def test_a_fee_payment_waits_for_the_pin_and_the_link_goes_to_the_main_payer(conn, provider):
    # Bug caught: a payment that is not held for the PIN, or a link nobody receives.
    outcome = propose_fee_payment(conn, provider, fee_id=new_fee(conn))
    assert outcome.status == "awaiting_pin"
    assert proposal_row(conn, outcome.proposal_id)["status"] == "awaiting_pin"
    (message,) = texts(conn, "m_amina")
    assert message.startswith("Pay GBP 6,000.00 to University of Westbridge (costs KES 1,038,062.29)")
    assert re.search(r"Confirm with your PIN: https://karo\.example/confirm/\S+$", message)


def test_the_outcome_message_never_carries_the_confirm_link(conn, provider):
    # Bug caught: the AI relays the outcome message, so a link here would leak the PIN page to the chat.
    outcome = propose_fee_payment(conn, provider)
    assert "/confirm/" not in outcome.message
    assert "http" not in outcome.message


def test_nobody_but_the_main_payer_and_no_log_sees_the_link(conn, provider):
    # Bug caught: the confirm URL (it carries the token) leaking to a relative, the student or the audit log.
    propose_fee_payment(conn, provider)
    for other in ("m_baraka", "m_nakato", "m_jabali"):
        assert texts(conn, other) == []
    token = re.search(r"/confirm/(\S+)", texts(conn, "m_amina")[0]).group(1)
    logged = json.dumps(audit.events(conn, FAM))
    assert "/confirm/" not in logged
    assert token not in logged


def test_the_stored_digest_is_the_one_the_pin_will_approve(conn, provider):
    # Bug caught: a proposal stored with a digest that differs from its content, so the PIN approves nothing.
    outcome = propose_fee_payment(conn, provider)
    stored, row = store.get_proposal(conn, FAM, outcome.proposal_id)
    assert row["digest"] == stored.digest()
    assert stored.requested_by == "m_amina"


def test_the_proposal_and_every_rule_result_are_audited(conn, provider):
    # Bug caught: a decision with no record of which rules ran and why.
    outcome = propose_fee_payment(conn, provider)
    (created,) = events(conn, "proposal.created")
    assert created["data"]["kind"] == "pay_fee"
    assert created["data"]["requested_by"] == "m_amina"
    assert created["data"]["digest"] == proposal_row(conn, outcome.proposal_id)["digest"]
    (checked,) = events(conn, "rules.checked")
    assert checked["data"]["stage"] == "propose"
    rules_run = {r["rule"] for r in checked["data"]["results"]}
    assert {"role", "freeze", "trusted_payee", "quote"} <= rules_run
    assert all(set(r) == {"rule", "outcome", "reason"} for r in checked["data"]["results"])


def test_a_new_payee_wait_still_asks_for_the_pin_and_says_when_it_can_run(conn, provider):
    # Bug caught: a waiting payment told "confirm now" with no hint it can't run yet, or a time shown in UTC.
    details = {"country": "GB", "currency": "GBP", "sort_code": "11-22-33", "account_number": "87654321"}
    payee = add_trusted_payee(conn, "tp_hall", "Hall of Residence", details, NOW - timedelta(hours=1))
    outcome = propose_fee_payment(conn, provider, payee=payee, target="1000.00")
    assert outcome.status == "awaiting_pin"
    assert outcome.decision.verdict is Outcome.WAIT
    (message,) = texts(conn, "m_amina")
    assert "can run after Tue 20 Oct, 11:00" in message  # 08:00 UTC on the 20th is 11:00 in Nairobi
    assert "Confirm with your PIN:" in message
    assert "Tue 20 Oct, 11:00" in outcome.message


def test_a_changed_payee_is_blocked_and_the_main_payer_is_warned(conn, provider):
    # Bug caught: a fake invoice with new bank details being proposed without the family being told.
    fake = Payee.of(
        "University of Westbridge", country="GB", currency="GBP", sort_code="99-99-99", account_number="00000001"
    )
    outcome = propose_fee_payment(conn, provider, payee=fake)
    assert outcome.status == "blocked"
    assert proposal_row(conn, outcome.proposal_id)["status"] == "blocked"
    (warning,) = texts(conn, "m_amina")
    assert warning.startswith("Warning: ")
    assert "/confirm/" not in warning
    (raised,) = events(conn, "warning.raised")
    assert raised["data"]["rule"] == "trusted_payee"
    assert conn.execute("SELECT COUNT(*) FROM confirm_links").fetchone()[0] == 0


def test_a_blocked_proposal_says_why_and_gets_no_link(conn, provider):
    # Bug caught: the agent being told "done" for something the rules refused, or a PIN link for a blocked proposal.
    outcome = propose_fee_payment(conn, provider, actor="m_baraka")
    assert outcome.status == "blocked"
    assert "Only the main payer" in outcome.message
    assert conn.execute("SELECT COUNT(*) FROM confirm_links").fetchone()[0] == 0
    assert texts(conn, "m_amina") == []


def test_an_unknown_quote_is_blocked_and_the_message_says_it_was_not_found(conn):
    # Bug caught: a missing quote silently becoming "no quote", or an exception instead of a blocked outcome.
    outcome = proposals.propose(
        conn,
        family_id=FAM,
        actor=member(conn, "m_amina"),
        kind=Kind.PAY_FEE,
        now=NOW,
        base_url=BASE_URL,
        amount=gbp("6000.00"),
        payee=westbridge(conn),
        quote_id="q_does_not_exist",
    )
    assert outcome.status == "blocked"
    assert "quote" in outcome.message and "wasn't found" in outcome.message
    assert conn.execute("SELECT COUNT(*) FROM confirm_links").fetchone()[0] == 0


def test_another_familys_quote_is_not_found(conn, provider):
    # Bug caught: a proposal built on a quote that belongs to a different family.
    foreign = quotes.create_quote(
        conn, provider, family_id=OTHER, source_currency="KES", target=gbp("6000.00"), now=NOW
    )
    outcome = propose_fee_payment(conn, provider, quote_id=foreign.id)
    assert outcome.status == "blocked"
    assert "wasn't found" in outcome.message


def test_an_unknown_fee_is_blocked(conn, provider):
    # Bug caught: a proposal pointing at a fee that doesn't exist, or one that belongs to another family.
    outcome = propose_fee_payment(conn, provider, fee_id="fee_does_not_exist")
    assert outcome.status == "blocked"
    assert "fee wasn't found" in outcome.message


def test_a_fee_that_is_already_paid_is_blocked(conn, provider):
    # Bug caught: the same fee paid a second time through a second proposal.
    fee_id = new_fee(conn)
    conn.execute("UPDATE fees SET status = 'paid' WHERE id = ?", (fee_id,))
    outcome = propose_fee_payment(conn, provider, fee_id=fee_id)
    assert outcome.status == "blocked"
    assert "already been paid" in outcome.message


def test_adding_a_payee_without_naming_one_is_blocked(conn):
    # Bug caught: an ADD_PAYEE proposal with nothing to add reaching the PIN page.
    outcome = proposals.propose(
        conn, family_id=FAM, actor=member(conn, "m_amina"), kind=Kind.ADD_PAYEE, now=NOW, base_url=BASE_URL
    )
    assert outcome.status == "blocked"


def test_a_person_from_another_family_is_blocked(conn):
    # Bug caught: someone else's main payer proposing money moves for this family.
    outsider = member(conn, "m_wanjiru", OTHER)
    outcome = proposals.propose(conn, family_id=FAM, actor=outsider, kind=Kind.UNFREEZE, now=NOW, base_url=BASE_URL)
    assert outcome.status == "blocked"
    assert texts(conn, "m_amina") == []


# --- propose: kinds that need no PIN ----------------------------------------------------------


def test_a_relative_paying_their_own_share_is_confirmed_without_a_pin(conn):
    # Bug caught: a kind outside PIN_KINDS being held for a PIN, or left unconfirmed so it can never run.
    conn.execute(
        "INSERT INTO shares (id, family_id, fee_id, member_id, amount_minor, currency, created_at) "
        "VALUES ('sh_b', ?, NULL, 'm_baraka', 200000, 'GBP', ?)",
        (FAM, clock.iso(NOW)),
    )
    outcome = proposals.propose(
        conn,
        family_id=FAM,
        actor=member(conn, "m_baraka"),
        kind=Kind.CONTRIBUTE,
        now=NOW,
        base_url=BASE_URL,
        amount=gbp("500.00"),
        share_id="sh_b",
    )
    assert outcome.status == "confirmed"
    assert texts(conn, "m_amina") == []
    assert conn.execute("SELECT COUNT(*) FROM confirm_links").fetchone()[0] == 0


def test_an_allowance_run_that_must_wait_is_not_marked_confirmed(conn, provider):
    # Bug caught: a WAIT result on a no-PIN kind being treated as approved to run now.
    details = {"country": "GB", "currency": "GBP", "sort_code": "11-22-33", "account_number": "87654321"}
    payee = add_trusted_payee(conn, "tp_new", "Jabali Savings", details, NOW - timedelta(hours=1))
    conn.execute(
        "INSERT INTO standing_orders "
        "(id, family_id, payee_id, amount_minor, currency, day_of_month, approved_by, active) "
        "VALUES ('so_new', ?, 'tp_new', 50000, 'GBP', 1, 'm_amina', 1)",
        (FAM,),
    )
    quote = quotes.create_quote(conn, provider, family_id=FAM, source_currency="KES", target=gbp("500.00"), now=NOW)
    outcome = proposals.propose(
        conn,
        family_id=FAM,
        actor=member(conn, "m_amina"),
        kind=Kind.ALLOWANCE_RUN,
        now=NOW,
        base_url=BASE_URL,
        amount=quote.target,
        payee=payee,
        quote_id=quote.id,
        standing_order_id="so_new",
    )
    assert outcome.status == "waiting"
    assert outcome.decision.verdict is Outcome.WAIT


# --- summary ----------------------------------------------------------------------------------


def test_summary_of_a_fee_payment_names_the_amount_payee_and_cost(conn, provider):
    # Bug caught: a PIN page that doesn't show what the PIN approves.
    quote = quotes.create_quote(conn, provider, family_id=FAM, source_currency="KES", target=gbp("6000.00"), now=NOW)
    p = Proposal(
        id="prop_x",
        family_id=FAM,
        kind=Kind.PAY_FEE,
        requested_by="m_amina",
        amount=quote.target,
        payee=westbridge(conn),
        quote=quote,
    )
    state = store.load_state(conn, FAM, NOW)
    assert proposals.summary(p, state) == "Pay GBP 6,000.00 to University of Westbridge (costs KES 1,038,062.29)"


def test_summary_of_a_split_names_who_pays_what(conn):
    # Bug caught: a split summary with member ids, or with no amounts, that the main payer can't check.
    p = Proposal(
        id="prop_s",
        family_id=FAM,
        kind=Kind.SPLIT,
        requested_by="m_amina",
        amount=gbp("4000.00"),
        split=(("m_baraka", gbp("2500.00")), ("m_nakato", gbp("1500.00"))),
    )
    state = store.load_state(conn, FAM, NOW)
    text = proposals.summary(p, state)
    assert "GBP 4,000.00" in text
    assert "Baraka GBP 2,500.00" in text and "Nakato GBP 1,500.00" in text
    assert "m_baraka" not in text


def test_every_kind_has_a_summary(conn):
    # Bug caught: a new kind reaching the PIN page with a blank or crashing summary.
    state = store.load_state(conn, FAM, NOW)
    payee = westbridge(conn)
    for kind in Kind:
        p = Proposal(id="prop_k", family_id=FAM, kind=kind, requested_by="m_amina", amount=gbp("10.00"), payee=payee)
        assert proposals.summary(p, state).strip()


# --- freeze -----------------------------------------------------------------------------------


def frozen(conn) -> int:
    return conn.execute("SELECT frozen FROM families WHERE id = ?", (FAM,)).fetchone()["frozen"]


def test_any_family_member_can_freeze_and_the_main_payer_is_told(conn):
    # Bug caught: a relative's STOP being ignored, or the main payer never hearing that money was stopped.
    sentence = proposals.freeze(conn, family_id=FAM, actor=member(conn, "m_baraka"), now=NOW)
    assert frozen(conn) == 1
    assert "frozen" in sentence
    (note,) = texts(conn, "m_amina")
    assert "Baraka" in note and "unfreeze" in note
    (event,) = events(conn, "family.frozen")
    assert event["actor"] == "m_baraka"


def test_the_student_can_freeze_too(conn):
    # Bug caught: freeze limited to relatives, so the student can't stop a payment they spot.
    proposals.freeze(conn, family_id=FAM, actor=member(conn, "m_jabali"), now=NOW)
    assert frozen(conn) == 1


def test_freezing_twice_says_so_and_changes_nothing(conn):
    # Bug caught: a second STOP spamming the main payer and the log.
    proposals.freeze(conn, family_id=FAM, actor=member(conn, "m_baraka"), now=NOW)
    sentence = proposals.freeze(conn, family_id=FAM, actor=member(conn, "m_nakato"), now=NOW)
    assert "already frozen" in sentence
    assert len(events(conn, "family.frozen")) == 1
    assert len(texts(conn, "m_amina")) == 1


def test_someone_from_another_family_cannot_freeze(conn):
    # Bug caught: any known member id freezing any family.
    outsider = member(conn, "m_wanjiru", OTHER)
    sentence = proposals.freeze(conn, family_id=FAM, actor=outsider, now=NOW)
    assert frozen(conn) == 0
    assert events(conn, "family.frozen") == []
    assert "can't" in sentence


def test_freezing_one_family_leaves_the_other_running(conn):
    # Bug caught: the UPDATE missing its family filter.
    proposals.freeze(conn, family_id=FAM, actor=member(conn, "m_baraka"), now=NOW)
    other = conn.execute("SELECT frozen FROM families WHERE id = ?", (OTHER,)).fetchone()["frozen"]
    assert other == 0


def test_balances_are_unchanged_by_proposing(conn, provider):
    # Bug caught: proposing moving money. Only the executor may.
    ledger.post(conn, family_id=FAM, at=NOW, amount=kes("5000000"), kind="collection", ref="seed")
    propose_fee_payment(conn, provider)
    assert ledger.balance(conn, FAM, "KES") == kes("5000000")
    assert conn.execute("SELECT COUNT(*) FROM payouts").fetchone()[0] == 0
