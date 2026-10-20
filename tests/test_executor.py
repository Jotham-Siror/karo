"""The executor: the one place money leaves. Every test names the bug it would catch."""

import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from karo import approvals, audit, clock, db, executor, ledger, proposals, quotes, seed, store
from karo.domain import Kind, Payee
from karo.money import Money
from karo.providers.base import PayoutResult
from karo.providers.standin import StandInProvider

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam_otieno"
OTHER = "fam_mwangi"
PIN = "4821"
PEPPER = "test-pepper"
BASE_URL = "https://karo.example"
RATES = {
    ("KES", "GBP"): Decimal("0.00578"),
    ("UGX", "GBP"): Decimal("0.000183"),  # a split now asks Nakato in Uganda for her share
    ("GBP", "KES"): Decimal("173"),
}
FUNDS = Money.parse("5000000", "KES")
FEE_COST = Money.parse("1038062.29", "KES")  # GBP 6,000.00 at the stand-in rate, rounded up


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


class CrashingProvider:
    """The first `payout` raises like a dropped connection; later calls go through. With `sent=True` the inner
    provider got that first payment before the connection dropped."""

    name = "standin"

    def __init__(self, inner, *, sent: bool) -> None:
        self.inner = inner
        self.sent = sent
        self.payout_calls = 0
        self.status_calls = 0

    def payout(self, **kwargs) -> PayoutResult:
        self.payout_calls += 1
        if self.payout_calls > 1:
            return self.inner.payout(**kwargs)
        if self.sent:
            self.inner.payout(**kwargs)
        raise ConnectionError("connection dropped")

    def payout_status(self, **kwargs) -> PayoutResult:
        self.status_calls += 1
        return self.inner.payout_status(**kwargs)


class ScriptedProvider:
    """Answers payout and payout_status with whatever `status` is set to, and remembers what it was asked."""

    name = "scripted"

    def __init__(self, *, status: str = "succeeded", simulated: bool = False) -> None:
        self.status = status
        self.simulated = simulated
        self.payouts: list[dict] = []
        self.status_calls = 0

    def _result(self, reference: str) -> PayoutResult:
        return PayoutResult(reference, "SC-1", self.status, self.simulated, "scripted")

    def payout(self, *, reference, amount, debit_currency, payee, narration) -> PayoutResult:
        self.payouts.append(
            {
                "reference": reference,
                "amount": amount,
                "debit_currency": debit_currency,
                "payee": payee,
                "narration": narration,
            }
        )
        return self._result(reference)

    def payout_status(self, *, reference, provider_id) -> PayoutResult:
        self.status_calls += 1
        return self._result(reference)


class WatchingProvider:
    """Looks at the database from a second connection at the moment `payout` is called."""

    name = "standin"

    def __init__(self, inner: StandInProvider, path: str) -> None:
        self.inner = inner
        self.path = path
        self.rows_seen: int | None = None
        self.debit_seen: Money | None = None
        self.write_lock_free: bool | None = None

    def payout(self, **kwargs) -> PayoutResult:
        other = db.connect(self.path)
        try:
            self.rows_seen = other.execute("SELECT COUNT(*) FROM payouts").fetchone()[0]
            self.debit_seen = ledger.balance(other, FAM, "KES")
            other.execute("BEGIN IMMEDIATE")  # waits 5 s then fails if the executor still holds its write lock
            other.execute("ROLLBACK")
            self.write_lock_free = True
        finally:
            other.close()
        return self.inner.payout(**kwargs)

    def payout_status(self, **kwargs) -> PayoutResult:
        return self.inner.payout_status(**kwargs)


class RivalDuringPayout:
    """While the first caller's `payout` is in flight, a second execute for the same proposal runs."""

    name = "standin"

    def __init__(self, inner: StandInProvider, conn, proposal_id: str, rival_now: datetime) -> None:
        self.inner = inner
        self.conn = conn
        self.proposal_id = proposal_id
        self.rival_now = rival_now
        self.rival = None

    def payout(self, **kwargs) -> PayoutResult:
        self.rival = run(self.conn, self.inner, self.proposal_id, now=self.rival_now)
        return self.inner.payout(**kwargs)

    def payout_status(self, **kwargs) -> PayoutResult:
        return self.inner.payout_status(**kwargs)


def member(conn, member_id: str, family_id: str = FAM):
    return store.member(conn, family_id, member_id)


def trusted(conn, payee_id: str) -> Payee:
    state = store.load_state(conn, FAM, NOW)
    return next(t.payee for t in state.trusted_payees if t.id == payee_id)


def add_trusted_payee(conn, payee_id: str, name: str, details: dict, since: datetime) -> Payee:
    conn.execute(
        "INSERT INTO trusted_payees (id, family_id, name, details_json, trusted_since, source) "
        "VALUES (?, ?, ?, ?, ?, 'added')",
        (payee_id, FAM, name, json.dumps(details), clock.iso(since)),
    )
    return Payee.of(name, **details)


def fund(conn, amount: Money = FUNDS) -> None:
    """Tops the family up so it holds `amount` in all, counting the opening balance the seed already gave it."""
    ledger.post(conn, family_id=FAM, at=NOW, amount=amount - seed.OPENING_BALANCE, kind="collection", ref="test-funds")


def ledger_kinds(conn) -> list[str]:
    """Kinds of the ledger rows the code under test wrote, leaving out the seeded and the test's own funding."""
    rows = conn.execute("SELECT kind FROM ledger WHERE ref NOT IN ('test-funds', 'opening_balance') ORDER BY seq")
    return [r["kind"] for r in rows]


def new_fee(conn, amount: str = "6000.00", payee: Payee | None = None) -> str:
    return store.insert_fee(
        conn,
        family_id=FAM,
        payee=payee or trusted(conn, "tp_westbridge"),
        amount=gbp(amount),
        due_date=None,
        invoice_ref="INV-77",
        now=NOW,
    )


def make_quote(conn, provider, target: Money, now: datetime = NOW):
    return quotes.create_quote(conn, provider, family_id=FAM, source_currency="KES", target=target, now=now)


def propose(conn, provider, kind: Kind, *, actor: str = "m_amina", target: str | None = None, **fields):
    """Proposes as `actor`, getting a fresh quote first when a target amount is given."""
    if target is not None:
        quote = make_quote(conn, provider, gbp(target))
        fields.update(amount=quote.target, quote_id=quote.id)
    return proposals.propose(
        conn, family_id=FAM, actor=member(conn, actor), kind=kind, now=NOW, base_url=BASE_URL, **fields
    )


def propose_fee(conn, provider, *, fee_id: str | None = None, payee: Payee | None = None, target: str = "6000.00"):
    payee = payee or trusted(conn, "tp_westbridge")
    # A fee payment is refused without an invoice, so a test that doesn't care about the fee gets one that fits.
    return propose(
        conn, provider, Kind.PAY_FEE, target=target, payee=payee, fee_id=fee_id or new_fee(conn, target, payee)
    )


def confirm(conn, now: datetime = NOW) -> str:
    """Follows the newest confirm link in the main payer's outbox, as she would, and enters the PIN."""
    row = conn.execute(
        "SELECT text FROM outbox WHERE member_id = 'm_amina' AND text LIKE '%/confirm/%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    token = re.search(r"/confirm/(\S+)", row["text"]).group(1)
    result = approvals.confirm(conn, token=token, pin=PIN, pepper=PEPPER, now=now)
    assert result.status == "confirmed"
    return result.proposal_id


def run(conn, provider, proposal_id: str, *, now: datetime = NOW, family_id: str = FAM):
    return executor.execute(conn, provider, family_id=family_id, proposal_id=proposal_id, now=now, base_url=BASE_URL)


def confirmed_fee_payment(conn, provider, **kw) -> str:
    outcome = propose_fee(conn, provider, **kw)
    assert outcome.status == "awaiting_pin"
    return confirm(conn)


def texts(conn, member_id: str) -> list[str]:
    return [r["text"] for r in conn.execute("SELECT text FROM outbox WHERE member_id = ? ORDER BY id", (member_id,))]


def events(conn, name: str) -> list[dict]:
    return [e for e in audit.events(conn, FAM) if e["event"] == name]


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def status_of(conn, proposal_id: str) -> str:
    return store.get_proposal(conn, FAM, proposal_id)[1]["status"]


def tamper(conn, proposal_id: str, **changes) -> None:
    payload = json.loads(conn.execute("SELECT payload_json FROM proposals WHERE id = ?", (proposal_id,)).fetchone()[0])
    payload.update(changes)
    conn.execute("UPDATE proposals SET payload_json = ? WHERE id = ?", (json.dumps(payload), proposal_id))


# --- paying a fee -----------------------------------------------------------------------------


def test_a_confirmed_fee_payment_is_paid_once_and_recorded_everywhere(conn, provider):
    # Bug caught: any link in propose -> PIN -> execute -> debit -> fee paid -> receipt missing or out of step.
    fund(conn)
    fee_id = new_fee(conn)
    outcome = propose_fee(conn, provider, fee_id=fee_id)
    assert outcome.status == "awaiting_pin"
    assert "/confirm/" not in outcome.message
    assert "/confirm/" in texts(conn, "m_amina")[0]

    proposal_id = confirm(conn)
    assert proposal_id == outcome.proposal_id
    result = run(conn, provider, proposal_id)

    assert result.status == "executed"
    assert provider.payout_calls == 1
    payout = conn.execute("SELECT * FROM payouts").fetchone()
    assert count(conn, "payouts") == 1
    assert result.payout_id == payout["id"]
    assert (payout["status"], payout["kind"], payout["simulated"]) == ("succeeded", "pay_fee", 1)
    assert payout["reference"] == f"karo-{proposal_id}"
    assert payout["provider_id"] == f"SI-karo-{proposal_id}"
    assert (payout["amount_minor"], payout["currency"]) == (600000, "GBP")
    assert (payout["debit_minor"], payout["debit_currency"]) == (FEE_COST.minor, "KES")
    assert payout["home_value_minor"] == FEE_COST.minor
    assert ledger.balance(conn, FAM, "KES") == FUNDS - FEE_COST
    assert store.get_fee(conn, FAM, fee_id)["status"] == "paid"
    assert status_of(conn, proposal_id) == "executed"
    receipt = texts(conn, "m_amina")[-1]
    assert "Paid GBP 6,000.00 to University of Westbridge" in receipt
    assert "(simulated)" in receipt


def paid_fee(conn, provider):
    fund(conn)
    proposal_id = confirmed_fee_payment(conn, provider)
    return run(conn, provider, proposal_id)


def test_the_receipt_ends_with_the_audit_hash_that_closes_the_payout(conn, provider):
    # Bug caught: a "Log check" taken before the payout's audit row, so it points at the wrong place.
    paid_fee(conn, provider)
    receipt = texts(conn, "m_amina")[-1]
    code = re.search(r"Log check ([0-9a-f]{12})$", receipt).group(1)
    assert code == events(conn, "payout.sent")[0]["hash"][:12]
    assert code in {e["hash"][:12] for e in audit.events(conn)}


def test_the_receipt_does_not_break_the_audit_chain(conn, provider):
    # Bug caught: reading the head in a way that writes to the log, or out of step with it, so verify fails.
    paid_fee(conn, provider)
    assert audit.verify(conn) == (True, None)


def test_the_reply_to_the_person_is_the_same_receipt_as_the_outbox_message(conn, provider):
    # Bug caught: the web page showing one Log check and the main payer's message another.
    result = paid_fee(conn, provider)
    assert result.message == texts(conn, "m_amina")[-1]


def test_executing_twice_pays_once(conn, provider):
    # Bug caught: a retried or double-clicked confirm pays the university twice.
    fund(conn)
    proposal_id = confirmed_fee_payment(conn, provider)
    run(conn, provider, proposal_id)
    again = run(conn, provider, proposal_id)
    assert again.status == "not_confirmed"
    assert provider.payout_calls == 1
    assert count(conn, "payouts") == 1
    assert ledger.balance(conn, FAM, "KES") == FUNDS - FEE_COST


def test_a_retry_right_after_a_crash_leaves_the_payment_alone(conn, provider):
    # Bug caught: a retry that finds no record reads it as failed and refunds a payout that may still be in flight.
    fund(conn)
    crashing = CrashingProvider(provider, sent=False)
    proposal_id = confirmed_fee_payment(conn, provider)
    with pytest.raises(ConnectionError):
        run(conn, crashing, proposal_id)
    assert count(conn, "payouts") == 1  # the row and the debit were already committed
    before = (count(conn, "audit"), count(conn, "ledger"), conn.execute("SELECT updated_at FROM payouts").fetchone()[0])

    retry = run(conn, crashing, proposal_id, now=NOW + timedelta(seconds=119))

    assert retry.status == "pending"
    assert (crashing.payout_calls, crashing.status_calls) == (1, 1)
    after = (count(conn, "audit"), count(conn, "ledger"), conn.execute("SELECT updated_at FROM payouts").fetchone()[0])
    assert after == before
    assert ledger.balance(conn, FAM, "KES") == FUNDS - FEE_COST
    assert status_of(conn, proposal_id) == "confirmed"


def test_a_payment_lost_in_a_crash_is_sent_again_after_two_minutes_under_the_same_reference(conn, provider):
    # Bug caught: a payment that never reached the provider is never sent (the debit stands forever), or is
    # sent under a new reference that could pay twice.
    fund(conn)
    crashing = CrashingProvider(provider, sent=False)
    fee_id = new_fee(conn)
    proposal_id = confirmed_fee_payment(conn, provider, fee_id=fee_id)
    with pytest.raises(ConnectionError):
        run(conn, crashing, proposal_id)

    retry = run(conn, crashing, proposal_id, now=NOW + timedelta(minutes=2))

    assert retry.status == "executed"
    assert (crashing.payout_calls, crashing.status_calls) == (2, 1)
    assert provider.payout_calls == 1
    assert provider.payout_status(reference=f"karo-{proposal_id}", provider_id=None).status == "succeeded"
    assert count(conn, "payouts") == 1
    assert ledger.balance(conn, FAM, "KES") == FUNDS - FEE_COST
    kinds = ledger_kinds(conn)
    assert kinds == ["payout"]
    assert store.get_fee(conn, FAM, fee_id)["status"] == "paid"


def test_a_second_execute_during_an_in_flight_payout_neither_refunds_nor_pays_twice(conn, provider):
    # Bug caught: the rival reads "no record yet" as failed and refunds, the payout then lands unrecorded,
    # the fee stays open and a new proposal pays it again.
    fund(conn)
    fee_id = new_fee(conn)
    proposal_id = confirmed_fee_payment(conn, provider, fee_id=fee_id)
    racing = RivalDuringPayout(provider, conn, proposal_id, rival_now=NOW + timedelta(seconds=30))

    first = run(conn, racing, proposal_id)

    assert racing.rival.status == "pending"
    assert first.status == "executed"
    assert provider.payout_calls == 1
    assert ledger.balance(conn, FAM, "KES") == FUNDS - FEE_COST
    assert ledger_kinds(conn) == ["payout"]
    assert status_of(conn, proposal_id) == "executed"
    assert store.get_fee(conn, FAM, fee_id)["status"] == "paid"
    assert propose_fee(conn, provider, fee_id=fee_id).status == "blocked"


def test_a_rival_that_sends_again_after_two_minutes_cannot_make_the_provider_pay_twice(conn, provider):
    # Bug caught: re-sending a slow payout without the same reference pays the fee twice, or records it twice.
    fund(conn)
    proposal_id = confirmed_fee_payment(conn, provider)
    racing = RivalDuringPayout(provider, conn, proposal_id, rival_now=NOW + timedelta(minutes=2))

    first = run(conn, racing, proposal_id)

    assert (racing.rival.status, first.status) == ("executed", "executed")
    assert provider.payout_calls == 1
    assert len(events(conn, "payout.sent")) == 1
    assert len([t for t in texts(conn, "m_amina") if t.startswith("Paid ")]) == 1
    assert ledger.balance(conn, FAM, "KES") == FUNDS - FEE_COST


def test_a_payout_the_provider_knows_is_never_sent_again_however_old(conn, provider):
    # Bug caught: re-sending a payment the provider already has, only because it is still pending after a while.
    fund(conn)
    scripted = ScriptedProvider(status="pending")
    proposal_id = confirmed_fee_payment(conn, provider)
    assert run(conn, scripted, proposal_id).status == "pending"

    again = run(conn, scripted, proposal_id, now=NOW + timedelta(hours=1))

    assert again.status == "pending"
    assert len(scripted.payouts) == 1
    assert ledger.balance(conn, FAM, "KES") == FUNDS - FEE_COST


def test_a_payout_the_provider_reports_failed_is_refunded_on_a_later_check(conn, provider):
    # Bug caught: a payout the provider says failed leaving the family out of pocket.
    fund(conn)
    scripted = ScriptedProvider(status="pending")
    proposal_id = confirmed_fee_payment(conn, provider)
    run(conn, scripted, proposal_id)
    scripted.status = "failed"

    result = run(conn, scripted, proposal_id, now=NOW + timedelta(minutes=1))

    assert result.status == "failed"
    assert ledger.balance(conn, FAM, "KES") == FUNDS
    assert status_of(conn, proposal_id) == "failed"


def test_a_freeze_holds_the_re_send_of_a_lost_payment(conn, provider):
    # Bug caught: money leaving after STOP because the re-send after a crash skipped the freeze.
    fund(conn)
    crashing = CrashingProvider(provider, sent=False)
    proposal_id = confirmed_fee_payment(conn, provider)
    with pytest.raises(ConnectionError):
        run(conn, crashing, proposal_id)
    proposals.freeze(conn, family_id=FAM, actor=member(conn, "m_baraka"), now=NOW)
    later = NOW + timedelta(minutes=5)

    held = run(conn, crashing, proposal_id, now=later)

    assert held.status == "blocked"
    assert crashing.payout_calls == 1
    assert ledger.balance(conn, FAM, "KES") == FUNDS - FEE_COST
    conn.execute("UPDATE families SET frozen = 0 WHERE id = ?", (FAM,))
    assert run(conn, crashing, proposal_id, now=later).status == "executed"
    assert provider.payout_calls == 1


def test_a_crash_after_the_provider_paid_is_settled_without_paying_again(conn, provider):
    # Bug caught: the provider paid but we lost the answer; the retry pays the university a second time.
    fund(conn)
    crashing = CrashingProvider(provider, sent=True)
    fee_id = new_fee(conn)
    proposal_id = confirmed_fee_payment(conn, provider, fee_id=fee_id)
    with pytest.raises(ConnectionError):
        run(conn, crashing, proposal_id)

    retry = run(conn, crashing, proposal_id)

    assert retry.status == "executed"
    assert provider.payout_calls == 1
    assert crashing.payout_calls == 1
    assert count(conn, "payouts") == 1
    assert conn.execute("SELECT status FROM payouts").fetchone()[0] == "succeeded"
    assert ledger.balance(conn, FAM, "KES") == FUNDS - FEE_COST
    assert store.get_fee(conn, FAM, fee_id)["status"] == "paid"


def test_a_retry_still_reconciles_when_the_debit_leaves_too_little_for_the_rules(conn, provider):
    # Bug caught: re-running the funds and daily-cap rules on a retry counts the same payment twice, so the
    # reconcile is blocked and a payment the provider made is never recorded.
    fund(conn, FEE_COST)  # exactly enough: after the debit the balance is zero
    crashing = CrashingProvider(provider, sent=True)
    proposal_id = confirmed_fee_payment(conn, provider)
    with pytest.raises(ConnectionError):
        run(conn, crashing, proposal_id)
    assert run(conn, crashing, proposal_id).status == "executed"


def test_two_callers_settling_the_same_failed_payout_return_the_money_once(conn, provider):
    # Bug caught: a retry racing another retry gives the debit back twice, so the family's balance grows.
    fund(conn)
    scripted = ScriptedProvider(status="failed")
    crashing = CrashingProvider(scripted, sent=False)
    proposal_id = confirmed_fee_payment(conn, provider)
    with pytest.raises(ConnectionError):
        run(conn, crashing, proposal_id)

    class Rival:
        """While this caller waits for the provider's answer, a rival caller settles the same payout first."""

        name = "scripted"

        def payout_status(self, **kwargs) -> PayoutResult:
            assert run(conn, scripted, proposal_id).status == "failed"
            return scripted.payout_status(**kwargs)

    assert run(conn, Rival(), proposal_id).status == "failed"
    assert ledger.balance(conn, FAM, "KES") == FUNDS


def test_the_payout_is_committed_before_the_provider_hears_about_it(conn, provider):
    # Bug caught: calling the provider inside the transaction, so a crash rolls back the row and the retry pays again.
    fund(conn)
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    watching = WatchingProvider(provider, path)
    run(conn, watching, confirmed_fee_payment(conn, provider))
    assert watching.rows_seen == 1
    assert watching.debit_seen == FUNDS - FEE_COST
    assert watching.write_lock_free is True


def test_execute_refuses_to_run_inside_a_callers_transaction(conn, provider):
    # Bug caught: joined to an outer transaction the payout row isn't committed before the provider call,
    # so a crash rolls it back and a retry pays again.
    fund(conn)
    proposal_id = confirmed_fee_payment(conn, provider)
    with db.transaction(conn), pytest.raises(RuntimeError):
        run(conn, provider, proposal_id)
    assert provider.payout_calls == 0


def test_an_unconfirmed_proposal_will_not_execute(conn, provider):
    # Bug caught: a payment running before the main payer has entered her PIN.
    fund(conn)
    outcome = propose_fee(conn, provider)
    result = run(conn, provider, outcome.proposal_id)
    assert result.status == "not_confirmed"
    assert provider.payout_calls == 0
    assert count(conn, "payouts") == 0
    assert ledger.balance(conn, FAM, "KES") == FUNDS


def test_a_status_forced_to_confirmed_without_a_pin_is_still_blocked(conn, provider):
    # Bug caught: the executor trusting the status column instead of re-checking the rules for a PIN.
    fund(conn)
    outcome = propose_fee(conn, provider)
    conn.execute("UPDATE proposals SET status = 'confirmed' WHERE id = ?", (outcome.proposal_id,))
    result = run(conn, provider, outcome.proposal_id)
    assert result.status == "blocked"
    assert provider.payout_calls == 0
    assert count(conn, "payouts") == 0


def test_a_proposal_edited_after_the_pin_is_blocked(conn, provider):
    # Bug caught: the executor building its own confirmation instead of using the one the PIN produced.
    fund(conn)
    proposal_id = confirmed_fee_payment(conn, provider)
    tamper(conn, proposal_id, requested_by="m_baraka")
    result = run(conn, provider, proposal_id)
    assert result.status == "blocked"
    assert provider.payout_calls == 0


def test_a_freeze_after_the_pin_blocks_the_payment_and_tells_the_main_payer(conn, provider):
    # Bug caught: money leaving after STOP because only the propose-time check ran.
    fund(conn)
    proposal_id = confirmed_fee_payment(conn, provider)
    proposals.freeze(conn, family_id=FAM, actor=member(conn, "m_baraka"), now=NOW)

    result = run(conn, provider, proposal_id)

    assert result.status == "blocked"
    assert "frozen" in result.message
    assert provider.payout_calls == 0
    assert count(conn, "payouts") == 0
    assert ledger.balance(conn, FAM, "KES") == FUNDS
    assert status_of(conn, proposal_id) == "blocked"
    assert "frozen" in texts(conn, "m_amina")[-1]
    checked = [e for e in events(conn, "rules.checked") if e["data"]["stage"] == "execute"]
    assert [r["rule"] for r in checked[0]["data"]["results"] if r["outcome"] == "block"] == ["freeze"]


def test_a_payment_blocked_at_execution_needs_a_fresh_pin_once_the_freeze_is_lifted(conn, provider):
    # Bug caught: a freeze then an unfreeze inside the quote window lets a payment run on a PIN given before the
    # freeze, with no new decision from the main payer.
    fund(conn)
    proposal_id = confirmed_fee_payment(conn, provider)
    proposals.freeze(conn, family_id=FAM, actor=member(conn, "m_baraka"), now=NOW)
    assert run(conn, provider, proposal_id).status == "blocked"
    conn.execute("UPDATE families SET frozen = 0 WHERE id = ?", (FAM,))

    again = run(conn, provider, proposal_id)

    assert again.status == "not_confirmed"
    assert provider.payout_calls == 0
    assert count(conn, "payouts") == 0


def test_an_expired_quote_is_rechecked_at_execution(conn, provider):
    # Bug caught: paying at a rate the provider no longer offers because only propose-time rules ran.
    fund(conn)
    proposal_id = confirmed_fee_payment(conn, provider)
    result = run(conn, provider, proposal_id, now=NOW + timedelta(minutes=16))
    assert result.status == "blocked"
    assert "quote has expired" in result.message
    assert provider.payout_calls == 0


def test_not_enough_funds_blocks_at_execution(conn, provider):
    # Bug caught: a payment sent without checking the ledger, overdrawing the family.
    proposal_id = confirmed_fee_payment(conn, provider)
    result = run(conn, provider, proposal_id)
    assert result.status == "blocked"
    assert "Not enough funds" in result.message
    assert provider.payout_calls == 0
    assert count(conn, "payouts") == 0


def test_a_new_payee_still_inside_its_wait_does_not_run(conn, provider):
    # Bug caught: the 24-hour new-payee wait enforced when proposing but not when paying.
    fund(conn)
    details = {"country": "GB", "currency": "GBP", "sort_code": "11-22-33", "account_number": "87654321"}
    hall = add_trusted_payee(conn, "tp_hall", "Hall of Residence", details, NOW - timedelta(hours=1))
    outcome = propose_fee(conn, provider, payee=hall, target="1000.00")
    proposal_id = confirm(conn)
    assert proposal_id == outcome.proposal_id

    result = run(conn, provider, proposal_id)

    assert result.status == "waiting"
    assert "Tue 20 Oct, 11:00" in result.message
    assert provider.payout_calls == 0
    assert status_of(conn, proposal_id) == "waiting"
    assert run(conn, provider, proposal_id).status == "not_confirmed"
    assert "new payee" in texts(conn, "m_amina")[-1]


def test_the_provider_is_paid_the_stored_payee_not_the_invoices_version(conn, provider):
    # Bug caught: sending the invoice's text of the payee (name, spacing) to the bank instead of the vetted record.
    fund(conn)
    scripted = ScriptedProvider()
    claim = Payee.of(
        "UNIVERSITY OF WESTBRIDGE (Fees Office)",
        country="gb",
        currency="GBP",
        sort_code="204060",
        account_number="4123 4567",
    )
    proposal_id = confirmed_fee_payment(conn, provider, payee=claim)
    run(conn, scripted, proposal_id)
    (sent,) = scripted.payouts
    assert sent["payee"] == trusted(conn, "tp_westbridge")
    assert sent["payee"].name == "University of Westbridge"


def test_the_payout_carries_the_references_and_the_invoice_number(conn, provider):
    # Bug caught: a payment the university can't match to an invoice, or one that doesn't use our idempotent reference.
    fund(conn)
    scripted = ScriptedProvider()
    proposal_id = confirmed_fee_payment(conn, provider, fee_id=new_fee(conn))
    run(conn, scripted, proposal_id)
    (sent,) = scripted.payouts
    assert sent["reference"] == f"karo-{proposal_id}"
    assert sent["amount"] == gbp("6000.00")
    assert sent["debit_currency"] == "KES"
    assert "INV-77" in sent["narration"]


def test_a_failed_payout_gives_the_money_back_and_leaves_the_fee_open(conn, provider):
    # Bug caught: a rejected payout that still costs the family, or marks the fee paid.
    fund(conn)
    scripted = ScriptedProvider(status="failed")
    fee_id = new_fee(conn)
    proposal_id = confirmed_fee_payment(conn, provider, fee_id=fee_id)

    result = run(conn, scripted, proposal_id)

    assert result.status == "failed"
    assert ledger.balance(conn, FAM, "KES") == FUNDS
    kinds = ledger_kinds(conn)
    assert kinds == ["payout", "payout_reversal"]
    assert store.get_fee(conn, FAM, fee_id)["status"] == "open"
    assert status_of(conn, proposal_id) == "failed"
    assert "failed" in texts(conn, "m_amina")[-1]
    assert [e["event"] for e in audit.events(conn, FAM)].count("payout.failed") == 1
    assert run(conn, scripted, proposal_id).status == "not_confirmed"


def test_a_pending_payout_stays_open_until_the_provider_says_it_landed(conn, provider):
    # Bug caught: a payout still in flight reported as done, or retried as a new payment.
    fund(conn)
    scripted = ScriptedProvider(status="pending")
    fee_id = new_fee(conn)
    proposal_id = confirmed_fee_payment(conn, provider, fee_id=fee_id)

    first = run(conn, scripted, proposal_id)
    assert first.status == "pending"
    assert status_of(conn, proposal_id) == "confirmed"
    assert store.get_fee(conn, FAM, fee_id)["status"] == "open"

    scripted.status = "succeeded"
    second = run(conn, scripted, proposal_id)

    assert second.status == "executed"
    assert len(scripted.payouts) == 1
    assert scripted.status_calls == 1
    assert store.get_fee(conn, FAM, fee_id)["status"] == "paid"
    assert conn.execute("SELECT provider_id FROM payouts").fetchone()[0] == "SC-1"


def test_a_receipt_says_simulated_only_when_the_provider_did(conn, provider):
    # Bug caught: a real payout labelled simulated, or a simulated one passed off as real.
    fund(conn)
    scripted = ScriptedProvider(simulated=False)
    run(conn, scripted, confirmed_fee_payment(conn, provider))
    receipt = texts(conn, "m_amina")[-1]
    assert "SC-1" in receipt
    assert "simulated" not in receipt


def test_a_fee_cannot_be_paid_a_second_time_by_another_proposal(conn, provider):
    # Bug caught: two proposals for one invoice, both confirmed, both paid.
    fund(conn)
    fee_id = new_fee(conn)
    first = confirmed_fee_payment(conn, provider, fee_id=fee_id)
    second_outcome = propose_fee(conn, provider, fee_id=fee_id)
    second = confirm(conn)
    assert second == second_outcome.proposal_id
    assert run(conn, provider, first).status == "executed"

    result = run(conn, provider, second)

    assert result.status == "blocked"
    assert "already been paid" in result.message
    assert provider.payout_calls == 1


def test_a_fee_with_a_payment_still_in_flight_cannot_be_paid_again(conn, provider):
    # Bug caught: a second proposal for a fee whose first payout is pending (fee not yet marked paid) pays it twice.
    fund(conn)
    scripted = ScriptedProvider(status="pending")
    fee_id = new_fee(conn)
    first = confirmed_fee_payment(conn, provider, fee_id=fee_id)
    second_outcome = propose_fee(conn, provider, fee_id=fee_id)
    second = confirm(conn)
    assert second == second_outcome.proposal_id
    assert run(conn, scripted, first).status == "pending"

    result = run(conn, scripted, second)

    assert result.status == "blocked"
    assert "already under way" in result.message
    assert len(scripted.payouts) == 1
    assert propose_fee(conn, provider, fee_id=fee_id).status == "blocked"


def test_another_familys_executor_call_finds_nothing(conn, provider):
    # Bug caught: executing a proposal by id alone, across families.
    fund(conn)
    proposal_id = confirmed_fee_payment(conn, provider)
    result = run(conn, provider, proposal_id, family_id=OTHER)
    assert result.status == "not_confirmed"
    assert provider.payout_calls == 0
    assert status_of(conn, proposal_id) == "confirmed"


def test_no_confirm_link_or_token_reaches_the_audit_log_or_a_receipt(conn, provider):
    # Bug caught: the executor copying a link or token into the log, or into the message sent to the family.
    fund(conn)
    proposal_id = confirmed_fee_payment(conn, provider)
    token = re.search(r"/confirm/(\S+)", texts(conn, "m_amina")[0]).group(1)
    run(conn, provider, proposal_id)
    logged = json.dumps(audit.events(conn, FAM))
    assert "/confirm/" not in logged and token not in logged
    assert "/confirm/" not in texts(conn, "m_amina")[-1]


def test_the_payment_is_audited_with_its_reference(conn, provider):
    # Bug caught: a payout with no trace of the provider's reference in the log.
    fund(conn)
    proposal_id = confirmed_fee_payment(conn, provider)
    run(conn, provider, proposal_id)
    (sent,) = events(conn, "payout.sent")
    assert sent["data"]["reference"] == f"karo-{proposal_id}"
    assert sent["data"]["provider_id"] == f"SI-karo-{proposal_id}"
    assert (sent["data"]["status"], sent["data"]["simulated"]) == ("succeeded", True)


# --- the other kinds --------------------------------------------------------------------------


def test_a_split_becomes_shares_that_add_up_to_the_fee(conn, provider):
    # Bug caught: shares that don't sum to the fee, or a fee left open while relatives are being asked.
    fee_id = new_fee(conn)
    outcome = propose(
        conn,
        provider,
        Kind.SPLIT,
        amount=gbp("6000.00"),
        fee_id=fee_id,
        split=(("m_baraka", gbp("3500.00")), ("m_nakato", gbp("2500.00"))),
    )
    assert outcome.status == "awaiting_pin"
    result = run(conn, provider, confirm(conn))

    assert result.status == "executed"
    shares = store.shares_for_fee(conn, FAM, fee_id)
    assert [(s["member_id"], s["amount_minor"], s["currency"], s["paid_minor"]) for s in shares] == [
        ("m_baraka", 350000, "GBP", 0),
        ("m_nakato", 250000, "GBP", 0),
    ]
    assert sum(s["amount_minor"] for s in shares) == store.get_fee(conn, FAM, fee_id)["amount_minor"]
    assert store.get_fee(conn, FAM, fee_id)["status"] == "collecting"
    assert status_of(conn, outcome.proposal_id) == "executed"
    assert len(events(conn, "split.created")) == 1


def test_a_second_split_confirmed_before_the_first_ran_is_blocked_at_execution(conn, provider):
    # Bug caught: relatives asked for the same fee twice because the second split was only checked when proposed.
    fee_id = new_fee(conn)
    split = (("m_baraka", gbp("3500.00")), ("m_nakato", gbp("2500.00")))
    propose(conn, provider, Kind.SPLIT, amount=gbp("6000.00"), fee_id=fee_id, split=split)
    first = confirm(conn)
    propose(conn, provider, Kind.SPLIT, amount=gbp("6000.00"), fee_id=fee_id, split=split)
    second = confirm(conn)
    assert run(conn, provider, first).status == "executed"

    result = run(conn, provider, second)

    assert result.status == "blocked"
    assert "already been asked" in result.message
    assert len(store.shares_for_fee(conn, FAM, fee_id)) == 2
    assert status_of(conn, second) == "blocked"


def test_a_fee_edited_after_the_proposal_blocks_the_payment(conn, provider):
    # Bug caught: paying an amount that no longer matches the invoice because the fee was only checked when proposed.
    fund(conn)
    fee_id = new_fee(conn)
    proposal_id = confirmed_fee_payment(conn, provider, fee_id=fee_id)
    conn.execute("UPDATE fees SET amount_minor = 700000 WHERE id = ?", (fee_id,))

    result = run(conn, provider, proposal_id)

    assert result.status == "blocked"
    assert "full amount" in result.message
    assert provider.payout_calls == 0


def test_a_fee_whose_payee_is_changed_after_the_proposal_blocks_the_payment(conn, provider):
    # Bug caught: paying the university's account for an invoice that now names someone else, because the
    # payee was only matched to the fee when proposed.
    fund(conn)
    fee_id = new_fee(conn)
    proposal_id = confirmed_fee_payment(conn, provider, fee_id=fee_id)
    son = trusted(conn, "tp_jabali")
    conn.execute(
        "UPDATE fees SET payee_name = ?, payee_details_json = ? WHERE id = ?",
        (son.name, json.dumps(dict(son.details)), fee_id),
    )

    result = run(conn, provider, proposal_id)

    assert result.status == "blocked"
    assert "payee doesn't match the invoice for this fee" in result.message
    assert provider.payout_calls == 0


def test_each_relative_is_told_their_own_share(conn, provider):
    # Bug caught: relatives not told what they owe, or told someone else's amount.
    outcome = propose(
        conn,
        provider,
        Kind.SPLIT,
        amount=gbp("6000.00"),
        fee_id=new_fee(conn),
        split=(("m_baraka", gbp("3500.00")), ("m_nakato", gbp("2500.00"))),
    )
    run(conn, provider, confirm(conn))
    # Two messages each now: the ask, then the payment link that the ask promises.
    to_baraka, to_nakato = texts(conn, "m_baraka"), texts(conn, "m_nakato")
    assert len(to_baraka) == 2 and len(to_nakato) == 2
    assert "GBP 3,500.00" in to_baraka[0] and "GBP 2,500.00" not in " ".join(to_baraka)
    assert "GBP 2,500.00" in to_nakato[0] and "GBP 3,500.00" not in " ".join(to_nakato)
    assert texts(conn, "m_jabali") == []
    assert outcome.status == "awaiting_pin"


def test_unfreeze_works_only_after_the_pin(conn, provider):
    # Bug caught: Karo unfreezing without the main payer's PIN.
    proposals.freeze(conn, family_id=FAM, actor=member(conn, "m_baraka"), now=NOW)
    outcome = propose(conn, provider, Kind.UNFREEZE)
    assert outcome.status == "awaiting_pin"

    assert run(conn, provider, outcome.proposal_id).status == "not_confirmed"
    assert conn.execute("SELECT frozen FROM families WHERE id = ?", (FAM,)).fetchone()[0] == 1

    confirm(conn)
    assert run(conn, provider, outcome.proposal_id).status == "executed"
    assert conn.execute("SELECT frozen FROM families WHERE id = ?", (FAM,)).fetchone()[0] == 0
    (event,) = events(conn, "family.unfrozen")
    assert event["actor"] == "m_amina"


def test_a_relative_cannot_propose_an_unfreeze(conn, provider):
    # Bug caught: a relative undoing a freeze the main payer wanted.
    proposals.freeze(conn, family_id=FAM, actor=member(conn, "m_baraka"), now=NOW)
    assert propose(conn, provider, Kind.UNFREEZE, actor="m_baraka").status == "blocked"


def test_a_standing_allowance_is_set_once_and_then_runs_without_a_pin(conn, provider):
    # Bug caught: an allowance run demanding a PIN every month, or running against an order nobody approved.
    fund(conn)
    jabali = trusted(conn, "tp_jabali")
    setup = propose(conn, provider, Kind.ALLOWANCE_SET, target="500.00", payee=jabali)
    assert setup.status == "awaiting_pin"
    assert run(conn, provider, confirm(conn)).status == "executed"
    order = conn.execute("SELECT * FROM standing_orders WHERE active = 1").fetchone()
    assert (order["payee_id"], order["amount_minor"], order["day_of_month"]) == ("tp_jabali", 50000, 1)
    assert (order["approved_by"], order["proposal_id"]) == ("m_amina", setup.proposal_id)

    due = propose(conn, provider, Kind.ALLOWANCE_RUN, target="500.00", payee=jabali, standing_order_id=order["id"])
    assert due.status == "confirmed"
    result = run(conn, provider, due.proposal_id)

    assert result.status == "executed"
    assert conn.execute("SELECT kind FROM payouts").fetchone()[0] == "allowance_run"
    assert count(conn, "confirmations") == 1  # only the setup was ever confirmed with a PIN


def test_a_new_allowance_replaces_the_old_one(conn, provider):
    # Bug caught: two active standing orders, so the student is paid twice a month.
    for amount in ("500.00", "400.00"):
        propose(conn, provider, Kind.ALLOWANCE_SET, target=amount, payee=trusted(conn, "tp_jabali"))
        run(conn, provider, confirm(conn))
    active = conn.execute("SELECT amount_minor FROM standing_orders WHERE active = 1").fetchall()
    assert [r[0] for r in active] == [40000]
    assert count(conn, "standing_orders") == 2


def test_a_confirmed_new_payee_is_added_as_trusted_from_now(conn, provider):
    # Bug caught: a payee added without the family's PIN, or added as long-trusted so the new-payee wait is skipped.
    payee = Payee.of("Hall of Residence", country="GB", currency="GBP", sort_code="11-22-33", account_number="87654321")
    outcome = propose(conn, provider, Kind.ADD_PAYEE, payee=payee)
    assert outcome.status == "awaiting_pin"
    assert conn.execute("SELECT COUNT(*) FROM trusted_payees WHERE name = 'Hall of Residence'").fetchone()[0] == 0

    assert run(conn, provider, confirm(conn)).status == "executed"

    row = conn.execute("SELECT * FROM trusted_payees WHERE name = 'Hall of Residence'").fetchone()
    assert (row["family_id"], row["source"], clock.parse(row["trusted_since"])) == (FAM, "added", NOW)
    state = store.load_state(conn, FAM, NOW)
    assert any(t.payee == payee for t in state.trusted_payees)


def test_a_conversion_moves_the_ledger_and_is_labelled_simulated(conn, provider):
    # Bug caught: a conversion that debits one currency without crediting the other.
    fund(conn)
    outcome = propose(conn, provider, Kind.CONVERT, target="1000.00")
    quote = store.get_proposal(conn, FAM, outcome.proposal_id)[0].quote
    assert run(conn, provider, confirm(conn)).status == "executed"
    assert ledger.balance(conn, FAM, "KES") == FUNDS - quote.source
    assert ledger.balance(conn, FAM, "GBP") == gbp("1000.00")
    (event,) = events(conn, "conversion.done")
    assert event["data"]["simulated"] is True
    assert provider.payout_calls == 0
