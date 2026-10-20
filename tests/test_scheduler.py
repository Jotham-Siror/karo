"""The scheduler's tick: allowance runs, reminders, waiting proposals and stuck payouts. Every test names the bug it
would catch."""

import json
import re
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from karo import approvals, audit, clock, collections, db, executor, ledger, proposals, quotes, scheduler, seed, store
from karo.domain import Kind, Payee
from karo.money import Money
from karo.providers.standin import StandInProvider

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam_otieno"
PIN = "4821"
PEPPER = "test-pepper"
BASE_URL = "https://karo.example"
HALL = {"country": "GB", "currency": "GBP", "sort_code": "11-22-33", "account_number": "87654321"}


def gbp(amount: str) -> Money:
    return Money.parse(amount, "GBP")


def kes(amount: str) -> Money:
    return Money.parse(amount, "KES")


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
def db_path(seeded_db, tmp_path):
    target = tmp_path / "karo.db"
    shutil.copyfile(seeded_db, target)
    return target


@pytest.fixture
def conn(db_path):
    connection = db.connect(db_path)
    yield connection
    connection.close()


@pytest.fixture
def provider() -> StandInProvider:
    return StandInProvider.from_config("standin-test-secret", BASE_URL)


class Wrapped:
    """The stand-in with some calls replaced. Everything else goes straight through."""

    name = "standin"

    def __init__(self, inner: StandInProvider) -> None:
        self.inner = inner

    def __getattr__(self, attr):
        return getattr(self.inner, attr)


class NoQuotes(Wrapped):
    """Every quote fails like a dropped connection, with a message that must never reach the audit log."""

    def quote(self, *args, **kwargs):
        raise ConnectionError("connection dropped: secret-text-xyz")


class DeadLinkCheck(Wrapped):
    """Asking about the listed payment links fails like a dropped connection."""

    def __init__(self, inner, tx_refs) -> None:
        super().__init__(inner)
        self.tx_refs = set(tx_refs)

    def verify_collection(self, tx_ref):
        if tx_ref in self.tx_refs:
            raise ConnectionError("connection dropped")
        return self.inner.verify_collection(tx_ref)


class DroppedAfterSending(Wrapped):
    """The first payout reaches the stand-in and then the connection drops, so the row is left pending."""

    def __init__(self, inner) -> None:
        super().__init__(inner)
        self.first = True
        self.status_calls = 0

    def payout(self, **kwargs):
        result = self.inner.payout(**kwargs)
        if self.first:
            self.first = False
            raise ConnectionError("connection dropped")
        return result

    def payout_status(self, **kwargs):
        self.status_calls += 1
        return self.inner.payout_status(**kwargs)


class Gated(Wrapped):
    """`quote` waits to be released, so a second tick can be started while the first is still inside it."""

    def __init__(self, inner) -> None:
        super().__init__(inner)
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def quote(self, *args, **kwargs):
        self.calls += 1
        self.entered.set()
        self.release.wait(10)
        return self.inner.quote(*args, **kwargs)


# --- setting things up --------------------------------------------------------------------------


def tick(conn, provider, now: datetime = NOW) -> None:
    scheduler.tick(conn, provider, now=now, base_url=BASE_URL)


def tick_now(conn, provider) -> None:
    """A tick at the demo clock's time, which moves when the clock is advanced."""
    tick(conn, provider, clock.now(conn))


def hours_later(conn, hours: int) -> None:
    clock.advance(conn, hours * 3600)


def fund(conn, amount: str = "5000000") -> None:
    """Tops the family up so it holds `amount` in all, counting the opening balance the seed already gave it."""
    ledger.post(
        conn, family_id=FAM, at=NOW, amount=kes(amount) - seed.OPENING_BALANCE, kind="collection", ref="test-funds"
    )


def add_trusted_payee(conn, payee_id: str, since: datetime) -> None:
    conn.execute(
        "INSERT INTO trusted_payees (id, family_id, name, details_json, trusted_since, source) "
        "VALUES (?, ?, 'Hall of Residence', ?, ?, 'added')",
        (payee_id, FAM, json.dumps(HALL), clock.iso(since)),
    )


def add_standing_order(conn, *, payee_id: str = "tp_jabali", amount: str = "500.00", day: int = 1) -> str:
    order_id = db.new_id("so")
    conn.execute(
        "INSERT INTO standing_orders (id, family_id, payee_id, amount_minor, currency, day_of_month, approved_by, "
        "active) VALUES (?, ?, ?, ?, 'GBP', ?, 'm_amina', 1)",
        (order_id, FAM, payee_id, gbp(amount).minor, day),
    )
    return order_id


def set_allowance_cap(conn, cap: Money) -> None:
    policy = json.loads(conn.execute("SELECT policy_json FROM families WHERE id = ?", (FAM,)).fetchone()[0])
    policy["allowance_monthly_cap"] = {"minor": cap.minor, "currency": cap.currency}
    conn.execute("UPDATE families SET policy_json = ? WHERE id = ?", (json.dumps(policy), FAM))


def trusted(conn, payee_id: str) -> Payee:
    return next(t.payee for t in store.load_state(conn, FAM, NOW).trusted_payees if t.id == payee_id)


def new_fee(conn, payee: Payee | None = None, amount: str = "6000.00") -> str:
    return store.insert_fee(
        conn,
        family_id=FAM,
        payee=payee or trusted(conn, "tp_westbridge"),
        amount=gbp(amount),
        due_date=None,
        invoice_ref="INV-77",
        now=NOW,
    )


def propose_fee(conn, provider, payee: Payee, target: str = "1000.00"):
    quote = quotes.create_quote(conn, provider, family_id=FAM, source_currency="KES", target=gbp(target), now=NOW)
    return proposals.propose(
        conn,
        family_id=FAM,
        actor=store.member(conn, FAM, "m_amina"),
        kind=Kind.PAY_FEE,
        now=NOW,
        base_url=BASE_URL,
        amount=quote.target,
        payee=payee,
        quote_id=quote.id,
        fee_id=new_fee(conn, payee, target),
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


def run(conn, provider, proposal_id: str, now: datetime = NOW):
    return executor.execute(conn, provider, family_id=FAM, proposal_id=proposal_id, now=now, base_url=BASE_URL)


def add_share(conn, fee_id: str, member_id: str, amount: str = "2000.00") -> str:
    share_id = db.new_id("sh")
    conn.execute(
        "INSERT INTO shares (id, family_id, fee_id, member_id, amount_minor, currency, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'GBP', ?)",
        (share_id, FAM, fee_id, member_id, gbp(amount).minor, clock.iso(NOW)),
    )
    return share_id


def owing_share(conn, provider, member_id: str, *, asked_at: datetime, fee_id: str | None = None) -> str:
    """A share whose payment link was asked for at `asked_at`."""
    share_id = add_share(conn, fee_id or new_fee(conn), member_id)
    asked = collections.request_share(
        conn, provider, family_id=FAM, share_id=share_id, now=asked_at, base_url=BASE_URL
    )
    assert asked is not None
    return share_id


def texts(conn, member_id: str) -> list[str]:
    return [r["text"] for r in conn.execute("SELECT text FROM outbox WHERE member_id = ? ORDER BY id", (member_id,))]


def reminders(conn, member_id: str) -> list[str]:
    return [t for t in texts(conn, member_id) if t.startswith("Reminder:")]


def events(conn, name: str) -> list[dict]:
    return [e for e in audit.events(conn, FAM) if e["event"] == name]


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def allowance_payouts(conn) -> list:
    return conn.execute("SELECT * FROM payouts WHERE kind = 'allowance_run' ORDER BY rowid").fetchall()


def proposal_statuses(conn, kind: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT status FROM proposals WHERE kind = ? ORDER BY rowid", (kind,))]


# --- allowance runs -----------------------------------------------------------------------------


def test_the_allowance_runs_once_in_a_month_however_many_ticks(conn, provider):
    # Catches: the same standing order paying the student every 30 seconds, or on every tick after the day arrives.
    fund(conn)
    add_standing_order(conn)
    tick(conn, provider)
    tick(conn, provider, NOW + timedelta(seconds=30))
    tick(conn, provider, NOW + timedelta(days=3))
    assert [r["status"] for r in allowance_payouts(conn)] == ["succeeded"]
    assert provider.payout_calls == 1
    assert proposal_statuses(conn, "allowance_run") == ["executed"]
    assert len([t for t in texts(conn, "m_amina") if t.startswith("Paid")]) == 1


def test_the_allowance_needs_no_pin_and_is_logged_as_scheduled(conn, provider):
    # Catches: a monthly run that asks for a PIN each month, or whose log reads as if the main payer asked for it.
    fund(conn)
    order_id = add_standing_order(conn)
    tick(conn, provider)
    assert count(conn, "confirmations") == 0
    assert count(conn, "confirm_links") == 0
    (scheduled,) = events(conn, "allowance.scheduled")
    assert scheduled["actor"] == "system"
    assert scheduled["data"]["standing_order_id"] == order_id


def test_the_allowance_runs_again_the_next_month(conn, provider):
    # Catches: "once a month" built as "once ever", so the student is paid in October and never again.
    fund(conn)
    add_standing_order(conn)
    tick(conn, provider)
    tick(conn, provider, datetime(2026, 11, 1, 6, 0, tzinfo=UTC))  # 09:00 on 1 November in Nairobi
    assert [r["status"] for r in allowance_payouts(conn)] == ["succeeded", "succeeded"]


def test_the_allowance_does_not_run_before_its_day(conn, provider):
    # Catches: a run on the 19th for an order set for the 25th.
    fund(conn)
    add_standing_order(conn, day=25)
    tick(conn, provider)
    assert allowance_payouts(conn) == []
    assert count(conn, "proposals") == 0
    tick(conn, provider, datetime(2026, 10, 25, 6, 0, tzinfo=UTC))
    assert len(allowance_payouts(conn)) == 1


def test_the_month_is_the_familys_local_month_not_utc(conn, provider):
    # Catches: a month cut at midnight UTC, so the November run is skipped for the first three hours of November.
    fund(conn)
    add_standing_order(conn)
    tick(conn, provider, datetime(2026, 10, 31, 20, 0, tzinfo=UTC))  # 23:00 on 31 October in Nairobi
    tick(conn, provider, datetime(2026, 10, 31, 21, 30, tzinfo=UTC))  # 00:30 on 1 November in Nairobi
    assert len(allowance_payouts(conn)) == 2


def test_the_allowance_respects_the_monthly_cap_and_tells_the_main_payer_once(conn, provider):
    # Catches: a run that pays past the monthly allowance limit, or one that fails in silence or nags every tick.
    fund(conn)
    add_standing_order(conn)
    set_allowance_cap(conn, kes("50000"))  # below what GBP 500 costs
    tick(conn, provider)
    tick(conn, provider, NOW + timedelta(minutes=1))
    assert allowance_payouts(conn) == []
    assert provider.payout_calls == 0
    assert ledger.balance(conn, FAM, "KES") == kes("5000000")
    assert proposal_statuses(conn, "allowance_run") == ["blocked"]
    (stopped,) = [t for t in texts(conn, "m_amina") if t.startswith("Stopped:")]
    assert "monthly allowance of GBP 500.00" in stopped
    assert "over the allowance limit of KES 50,000.00" in stopped


def test_a_run_left_confirmed_by_a_crash_is_still_paid(conn, provider):
    # Catches: a crash between making the run and executing it, which leaves the month's allowance unpaid for good.
    fund(conn)
    order_id = add_standing_order(conn)
    quote = quotes.create_quote(conn, provider, family_id=FAM, source_currency="KES", target=gbp("500.00"), now=NOW)
    left = proposals.propose(
        conn,
        family_id=FAM,
        actor=store.member(conn, FAM, "m_amina"),
        kind=Kind.ALLOWANCE_RUN,
        now=NOW,
        base_url=BASE_URL,
        amount=quote.target,
        payee=trusted(conn, "tp_jabali"),
        quote_id=quote.id,
        standing_order_id=order_id,
    )
    assert left.status == "confirmed"
    tick(conn, provider)
    assert proposal_statuses(conn, "allowance_run") == ["executed"]
    assert len(allowance_payouts(conn)) == 1


def test_a_run_for_a_new_payee_waits_and_then_goes_without_a_pin(conn, provider):
    # Catches: the new-payee wait skipped for the allowance, or a run that waits and is then never made.
    fund(conn)
    add_trusted_payee(conn, "tp_hall", since=NOW - timedelta(hours=1))
    add_standing_order(conn, payee_id="tp_hall")
    tick(conn, provider)
    assert proposal_statuses(conn, "allowance_run") == ["waiting"]
    assert len([t for t in texts(conn, "m_amina") if t.startswith("Not yet:")]) == 1

    tick(conn, provider, NOW + timedelta(hours=2))
    assert proposal_statuses(conn, "allowance_run") == ["waiting"]  # still inside the wait, and not made twice
    assert allowance_payouts(conn) == []

    tick(conn, provider, NOW + timedelta(hours=24))
    assert proposal_statuses(conn, "allowance_run") == ["failed", "executed"]
    assert len(allowance_payouts(conn)) == 1
    assert count(conn, "confirmations") == 0


def allowance_proposal(conn, provider, at: datetime) -> str:
    """The main payer asks for the GBP 500 allowance to the student at `at`. Returns the proposal id."""
    quote = quotes.create_quote(conn, provider, family_id=FAM, source_currency="KES", target=gbp("500.00"), now=at)
    outcome = proposals.propose(
        conn,
        family_id=FAM,
        actor=store.member(conn, FAM, "m_amina"),
        kind=Kind.ALLOWANCE_SET,
        now=at,
        base_url=BASE_URL,
        amount=quote.target,
        payee=trusted(conn, "tp_jabali"),
        quote_id=quote.id,
    )
    assert outcome.status == "awaiting_pin"
    return outcome.proposal_id


def approve_allowance(conn, provider, at: datetime) -> None:
    """The real way an allowance is set: asked for, confirmed with the PIN and put in place, all at `at`."""
    allowance_proposal(conn, provider, at)
    assert run(conn, provider, confirm(conn, at), at).status == "executed"


def test_an_allowance_set_on_the_20th_first_runs_on_the_1st_of_next_month(conn, provider):
    # Catches: "GBP 500 on the 1st" approved on the 20th being paid at the next tick, which the PIN page didn't say.
    fund(conn)
    approved = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    approve_allowance(conn, provider, approved)
    tick(conn, provider, approved)
    tick(conn, provider, datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    assert allowance_payouts(conn) == []
    assert count(conn, "proposals") == 1  # only the one that set it up: no run was even proposed
    tick(conn, provider, datetime(2026, 11, 1, 6, 0, tzinfo=UTC))
    (paid,) = allowance_payouts(conn)
    assert paid["created_at"].startswith("2026-11-01")


def test_an_allowance_set_today_waits_until_the_demo_clock_reaches_the_1st(conn, provider):
    # Catches: the first run counted from real time, or from the order's own day, so the demo jump never shows it.
    fund(conn)
    approve_allowance(conn, provider, clock.now(conn))
    tick_now(conn, provider)
    assert allowance_payouts(conn) == []
    hours_later(conn, 24 * 40)  # a clean month or more ahead: the 1st has certainly come
    tick_now(conn, provider)
    assert len(allowance_payouts(conn)) == 1


def test_an_allowance_approved_on_the_day_itself_waits_for_next_months_day(conn, provider):
    # Catches: approved on the 1st at 10:00 for "the 1st" and paid that same afternoon.
    fund(conn)
    approved = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)  # 10:00 on 1 November in Nairobi
    approve_allowance(conn, provider, approved)
    tick(conn, provider, approved + timedelta(minutes=1))
    tick(conn, provider, datetime(2026, 11, 30, 12, 0, tzinfo=UTC))
    assert allowance_payouts(conn) == []
    tick(conn, provider, datetime(2026, 12, 1, 6, 0, tzinfo=UTC))
    (paid,) = allowance_payouts(conn)
    assert paid["created_at"].startswith("2026-12-01")


def test_the_day_of_approval_is_the_familys_local_day_not_utc(conn, provider):
    # Catches: approval at 01:00 on 1 November in Nairobi counted as 31 October, so the 1st looked still to come.
    fund(conn)
    approve_allowance(conn, provider, datetime(2026, 10, 31, 22, 0, tzinfo=UTC))
    tick(conn, provider, datetime(2026, 11, 1, 6, 0, tzinfo=UTC))
    assert allowance_payouts(conn) == []
    tick(conn, provider, datetime(2026, 12, 1, 6, 0, tzinfo=UTC))
    assert len(allowance_payouts(conn)) == 1


def test_an_allowance_approved_last_month_runs_this_month(conn, provider):
    # Catches: a rule so strict that an allowance set on 20 September skips the 1st of October.
    fund(conn)
    approve_allowance(conn, provider, datetime(2026, 9, 20, 9, 0, tzinfo=UTC))
    tick(conn, provider, datetime(2026, 10, 1, 6, 0, tzinfo=UTC))
    (paid,) = allowance_payouts(conn)
    assert paid["created_at"].startswith("2026-10-01")


def test_an_order_with_no_recorded_confirmation_counts_from_when_it_was_proposed(conn, provider):
    # Catches: an order whose PIN record is missing being treated as approved long ago, and paid at once.
    fund(conn)
    proposed = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    proposal_id = allowance_proposal(conn, provider, proposed)
    order_id = add_standing_order(conn)
    conn.execute("UPDATE standing_orders SET proposal_id = ? WHERE id = ?", (proposal_id, order_id))
    assert count(conn, "confirmations") == 0
    tick(conn, provider, proposed + timedelta(hours=1))
    assert allowance_payouts(conn) == []
    tick(conn, provider, datetime(2026, 11, 1, 6, 0, tzinfo=UTC))
    assert len(allowance_payouts(conn)) == 1


def lapsed_notices(conn) -> list[str]:
    return [t for t in texts(conn, "m_amina") if "didn't run in time" in t]


def test_an_allowance_that_waited_into_the_next_month_is_skipped_not_paid_in_its_place(conn, provider):
    # Catches: October's late run being paid on 1 November, which fills November's slot so November is never paid.
    fund(conn)
    add_trusted_payee(conn, "tp_hall", since=NOW - timedelta(hours=1))
    add_standing_order(conn, payee_id="tp_hall")
    tick(conn, provider)
    (october,) = [r[0] for r in conn.execute("SELECT id FROM proposals WHERE kind = 'allowance_run'")]
    assert proposal_statuses(conn, "allowance_run") == ["waiting"]

    jump = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)  # 09:00 on 1 November in Nairobi, as the demo button lands
    tick(conn, provider, jump)
    tick(conn, provider, jump + timedelta(minutes=1))

    assert proposal_statuses(conn, "allowance_run") == ["failed", "executed"]
    assert events(conn, "proposal.superseded") == []
    (lapsed,) = events(conn, "allowance.lapsed")
    assert lapsed["data"] == {"month": "2026-10", "proposal_id": october}
    (paid,) = allowance_payouts(conn)
    assert paid["created_at"].startswith("2026-11-01")
    assert lapsed_notices(conn) == [
        "October's allowance didn't run in time, so it was skipped. November's runs as usual."
    ]


def test_a_late_run_from_last_month_does_not_add_a_second_payout_to_this_month(conn, provider):
    # Catches: a run waiting across midnight on 31 October being renewed beside November's own run, two in one month.
    fund(conn)
    add_trusted_payee(conn, "tp_hall", since=datetime(2026, 10, 31, 11, 0, tzinfo=UTC))  # 14:00 in Nairobi
    add_standing_order(conn, payee_id="tp_hall")
    tick(conn, provider, datetime(2026, 10, 31, 12, 0, tzinfo=UTC))  # 15:00 on 31 October: October's run waits a day
    tick(conn, provider, datetime(2026, 11, 1, 6, 0, tzinfo=UTC))  # 09:00 on 1 November: November's run waits too
    assert proposal_statuses(conn, "allowance_run") == ["waiting", "waiting"]

    tick(conn, provider, datetime(2026, 11, 1, 11, 30, tzinfo=UTC))  # 14:30: both waits are over

    assert proposal_statuses(conn, "allowance_run") == ["failed", "failed", "executed"]
    assert [e["data"]["month"] for e in events(conn, "allowance.lapsed")] == ["2026-10"]
    assert len(events(conn, "proposal.superseded")) == 1  # November's own run, renewed
    assert len(allowance_payouts(conn)) == 1
    assert len(lapsed_notices(conn)) == 1


# --- waiting proposals --------------------------------------------------------------------------


def waiting_fee_payment(conn, provider) -> str:
    """A fee payment to a payee trusted an hour ago: confirmed with the PIN, then stopped by the new-payee wait."""
    fund(conn)
    add_trusted_payee(conn, "tp_hall", since=NOW - timedelta(hours=1))
    outcome = propose_fee(conn, provider, trusted(conn, "tp_hall"))
    assert outcome.status == "awaiting_pin"
    proposal_id = confirm(conn)
    assert run(conn, provider, proposal_id).status == "waiting"
    return proposal_id


def test_a_waiting_payment_past_its_wait_is_replaced_by_one_that_needs_a_new_pin(conn, provider):
    # Catches: a payment that waited a day being paid on the old PIN, or never being offered again.
    old_id = waiting_fee_payment(conn, provider)
    old_quote = store.get_proposal(conn, FAM, old_id)[0].quote
    tick(conn, provider, NOW + timedelta(hours=1))
    assert proposal_statuses(conn, "pay_fee") == ["waiting"]  # the wait is not over, so nothing is done yet

    later = NOW + timedelta(hours=24)
    tick(conn, provider, later)
    assert proposal_statuses(conn, "pay_fee") == ["failed", "awaiting_pin"]
    (superseded,) = events(conn, "proposal.superseded")
    new_id = superseded["data"]["new_id"]
    assert superseded["data"] == {"old_id": old_id, "new_id": new_id}
    new, row = store.get_proposal(conn, FAM, new_id)
    assert row["status"] == "awaiting_pin"
    assert new.quote.id != old_quote.id
    assert new.quote.expires_at > later
    assert "new PIN confirmation" in texts(conn, "m_amina")[-1]

    assert confirm(conn, later) == new_id
    assert run(conn, provider, new_id, later).status == "executed"


def test_a_waiting_payment_is_replaced_once_however_many_ticks(conn, provider):
    # Catches: a fresh PIN link and a fresh quote every 30 seconds for one waiting payment.
    waiting_fee_payment(conn, provider)
    later = NOW + timedelta(hours=24)
    tick(conn, provider, later)
    tick(conn, provider, later + timedelta(seconds=30))
    tick(conn, provider, later + timedelta(hours=5))
    assert proposal_statuses(conn, "pay_fee") == ["failed", "awaiting_pin"]
    assert len(events(conn, "proposal.superseded")) == 1
    assert count(conn, "confirm_links") == 2


def test_a_waiting_payment_stays_waiting_if_the_quote_cannot_be_had(conn, provider):
    # Catches: the old request marked failed before its replacement exists, so a provider hiccup loses the payment.
    old_id = waiting_fee_payment(conn, provider)
    later = NOW + timedelta(hours=24)
    tick(conn, NoQuotes(provider), later)
    assert proposal_statuses(conn, "pay_fee") == ["waiting"]
    tick(conn, provider, later + timedelta(seconds=30))
    assert proposal_statuses(conn, "pay_fee") == ["failed", "awaiting_pin"]
    assert events(conn, "proposal.superseded")[0]["data"]["old_id"] == old_id


# --- reminders ----------------------------------------------------------------------------------


def test_a_reminder_goes_after_24_hours_and_at_most_once_a_day(conn, provider):
    # Catches: a reminder sent on the first tick, a missing reminder after a day, or one every 30 seconds.
    owing_share(conn, provider, "m_baraka", asked_at=clock.now(conn))
    tick_now(conn, provider)
    hours_later(conn, 23)
    tick_now(conn, provider)
    assert reminders(conn, "m_baraka") == []

    hours_later(conn, 2)  # 25 hours since the link was asked for
    tick_now(conn, provider)
    assert len(reminders(conn, "m_baraka")) == 1
    tick_now(conn, provider)
    hours_later(conn, 1)
    tick_now(conn, provider)
    assert len(reminders(conn, "m_baraka")) == 1

    hours_later(conn, 25)
    tick_now(conn, provider)
    assert len(reminders(conn, "m_baraka")) == 2


def test_a_reminder_goes_only_to_the_share_that_is_due(conn, provider):
    # Catches: one overdue share making the scheduler remind everyone on the fee, including people who just got a link.
    fee_id = new_fee(conn)
    owing_share(conn, provider, "m_baraka", asked_at=NOW - timedelta(hours=30), fee_id=fee_id)
    owing_share(conn, provider, "m_nakato", asked_at=NOW - timedelta(hours=1), fee_id=fee_id)
    paid = owing_share(conn, provider, "m_amina", asked_at=NOW - timedelta(hours=30), fee_id=fee_id)
    conn.execute("UPDATE shares SET paid_minor = amount_minor WHERE id = ?", (paid,))
    tick(conn, provider)
    assert len(reminders(conn, "m_baraka")) == 1
    assert reminders(conn, "m_nakato") == []
    assert reminders(conn, "m_amina") == []


def test_a_manual_reminder_counts_towards_the_day(conn, provider):
    # Catches: the scheduler reminding someone minutes after the main payer already did.
    fee_id = new_fee(conn)
    share_id = owing_share(conn, provider, "m_baraka", asked_at=NOW - timedelta(hours=30), fee_id=fee_id)
    collections.send_reminders(
        conn, provider, family_id=FAM, fee_id=fee_id, now=NOW - timedelta(hours=1), base_url=BASE_URL, actor="m_amina"
    )
    tick(conn, provider)
    assert len(reminders(conn, "m_baraka")) == 1
    assert [e["data"]["share_id"] for e in events(conn, "reminder.sent")] == [share_id]


# --- stuck payouts ------------------------------------------------------------------------------


def test_a_payout_left_pending_is_reconciled_once_it_is_old_enough(conn, provider):
    # Catches: a payment whose answer was lost staying pending for good, or a tick re-sending one still being sent.
    fund(conn)
    dropping = DroppedAfterSending(provider)
    outcome = propose_fee(conn, dropping, trusted(conn, "tp_westbridge"), target="6000.00")
    proposal_id = confirm(conn)
    assert proposal_id == outcome.proposal_id
    with pytest.raises(ConnectionError):
        run(conn, dropping, proposal_id)
    assert conn.execute("SELECT status FROM payouts").fetchone()[0] == "pending"

    tick(conn, dropping, NOW + timedelta(minutes=1))
    assert dropping.status_calls == 0
    assert conn.execute("SELECT status FROM payouts").fetchone()[0] == "pending"

    tick(conn, dropping, NOW + timedelta(minutes=3))
    assert conn.execute("SELECT status FROM payouts").fetchone()[0] == "succeeded"
    assert store.get_proposal(conn, FAM, proposal_id)[1]["status"] == "executed"
    assert provider.payout_calls == 1


# --- errors -------------------------------------------------------------------------------------


def test_a_provider_error_is_audited_without_its_message_and_the_rest_of_the_tick_goes_on(conn, provider):
    # Catches: one dropped connection stopping the reminders, or the error text (which may carry secrets) audited.
    fund(conn)
    add_standing_order(conn)
    owing_share(conn, provider, "m_baraka", asked_at=NOW - timedelta(hours=30))
    tick(conn, NoQuotes(provider))
    assert allowance_payouts(conn) == []
    assert len(reminders(conn, "m_baraka")) == 1
    (failed,) = events(conn, "scheduler.error")
    assert failed["data"]["step"] == "allowance"
    assert failed["data"]["error"] == "ConnectionError"
    assert "secret-text-xyz" not in json.dumps(audit.events(conn, None))


def test_a_provider_error_on_one_share_does_not_stop_the_others(conn, provider):
    # Catches: the first share that errors ending the reminders for everyone after it.
    fee_id = new_fee(conn)
    first = owing_share(conn, provider, "m_baraka", asked_at=NOW - timedelta(hours=30), fee_id=fee_id)
    owing_share(conn, provider, "m_nakato", asked_at=NOW - timedelta(hours=30), fee_id=fee_id)
    tx_ref = store.latest_pending_collection(conn, FAM, first)["tx_ref"]
    tick(conn, DeadLinkCheck(provider, [tx_ref]))
    assert reminders(conn, "m_baraka") == []
    assert len(reminders(conn, "m_nakato")) == 1
    (failed,) = events(conn, "scheduler.error")
    assert (failed["data"]["step"], failed["data"]["subject_id"]) == ("reminder", first)


# --- how a tick runs ----------------------------------------------------------------------------


def test_a_tick_refuses_to_run_inside_a_transaction(conn, provider):
    # Catches: provider calls made while the write lock is held, and errors swallowed that should have been raised.
    with db.transaction(conn):
        with pytest.raises(RuntimeError, match="transaction"):
            tick(conn, provider)


def test_two_ticks_at_once_run_the_allowance_once(conn, db_path, provider):
    # Catches: the demo button's tick overlapping the background tick, so both see no run yet and pay twice.
    fund(conn)
    add_standing_order(conn)
    gated = Gated(provider)
    failures = []

    def one_tick():
        connection = db.connect(db_path)
        try:
            tick(connection, gated)
        except Exception as error:  # reported on the test's thread, not lost in this one
            failures.append(error)
        finally:
            connection.close()

    first, second = threading.Thread(target=one_tick), threading.Thread(target=one_tick)
    try:
        first.start()
        assert gated.entered.wait(10)
        second.start()
        time.sleep(0.5)  # long enough for an unguarded second tick to reach its own quote
        assert gated.calls == 1
    finally:
        gated.release.set()
        for started in (first, second):
            if started.is_alive():
                started.join(20)

    assert failures == []
    assert len(allowance_payouts(conn)) == 1
    assert provider.payout_calls == 1
