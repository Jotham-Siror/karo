"""Collecting shares: payment links after a split, and reminders. Every test names the bug it would catch."""

import json
import re
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from karo import approvals, audit, collections, db, executor, proposals, seed, store
from karo.domain import Kind
from karo.money import Money
from karo.providers.standin import StandInProvider

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam_otieno"
OTHER = "fam_mwangi"
PIN = "4821"
PEPPER = "test-pepper"
BASE_URL = "https://karo.example"
SHARES = (("m_baraka", "3000.00"), ("m_nakato", "2000.00"), ("m_amina", "1000.00"))
LINKS_LATER = "Some payment links couldn't be sent yet; Karo will retry when you send reminders."


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
    # The demo's own rates, so a pair the demo needs but the config lacks fails here too.
    return StandInProvider.from_config("standin-test-secret", BASE_URL)


class Recording:
    """The stand-in, remembering each collection it was asked to create and whether a transaction was open then."""

    name = "standin"

    def __init__(self, inner: StandInProvider, conn) -> None:
        self.inner = inner
        self.conn = conn
        self.calls: list[dict] = []
        self.in_transaction: list[bool] = []

    def __getattr__(self, attr):
        return getattr(self.inner, attr)

    def create_collection(self, **kwargs):
        self.calls.append(kwargs)
        self.in_transaction.append(self.conn.in_transaction)
        return self.inner.create_collection(**kwargs)


class Flaky(Recording):
    """The `fail_on`th request for a link fails like a dropped connection. The others go through."""

    def __init__(self, inner, conn, *, fail_on: int) -> None:
        super().__init__(inner, conn)
        self.fail_on = fail_on

    def create_collection(self, **kwargs):
        if len(self.calls) + 1 == self.fail_on:
            self.calls.append(kwargs)
            raise ConnectionError("connection dropped")
        return super().create_collection(**kwargs)


def restarted() -> StandInProvider:
    """A stand-in that has lost every payment it knew of, as after a restart."""
    return StandInProvider.from_config("standin-test-secret", BASE_URL)


def new_fee(conn, family_id: str = FAM) -> str:
    state = store.load_state(conn, FAM, NOW)
    payee = next(t.payee for t in state.trusted_payees if t.id == "tp_westbridge")
    return store.insert_fee(
        conn, family_id=family_id, payee=payee, amount=gbp("6000.00"), due_date=None, invoice_ref="INV-77", now=NOW
    )


def add_share(conn, fee_id: str, member_id: str, amount: str, paid: str = "0") -> str:
    """A share row written directly, for tests of one function that don't need the PIN flow in front of it."""
    share_id = db.new_id("sh")
    conn.execute(
        "INSERT INTO shares (id, family_id, fee_id, member_id, amount_minor, currency, paid_minor, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'GBP', ?, ?)",
        (share_id, FAM, fee_id, member_id, gbp(amount).minor, gbp(paid).minor, "2026-10-19T09:00:00+00:00"),
    )
    return share_id


def request(conn, provider, share_id: str, family_id: str = FAM):
    return collections.request_share(
        conn, provider, family_id=family_id, share_id=share_id, now=NOW, base_url=BASE_URL
    )


def confirm(conn) -> str:
    """Follows the newest confirm link in the main payer's outbox, as she would, and enters the PIN."""
    row = conn.execute(
        "SELECT text FROM outbox WHERE member_id = 'm_amina' AND text LIKE '%/confirm/%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    token = re.search(r"/confirm/(\S+)", row["text"]).group(1)
    result = approvals.confirm(conn, token=token, pin=PIN, pepper=PEPPER, now=NOW)
    assert result.status == "confirmed"
    return result.proposal_id


def confirmed_split(conn, shares=SHARES) -> tuple[str, str]:
    fee_id = new_fee(conn)
    outcome = proposals.propose(
        conn,
        family_id=FAM,
        actor=store.member(conn, FAM, "m_amina"),
        kind=Kind.SPLIT,
        now=NOW,
        base_url=BASE_URL,
        amount=gbp("6000.00"),
        fee_id=fee_id,
        split=[(member_id, gbp(amount)) for member_id, amount in shares],
    )
    assert outcome.status == "awaiting_pin"
    return fee_id, confirm(conn)


def run(conn, provider, proposal_id: str):
    return executor.execute(conn, provider, family_id=FAM, proposal_id=proposal_id, now=NOW, base_url=BASE_URL)


def run_split(conn, provider, shares=SHARES) -> str:
    fee_id, proposal_id = confirmed_split(conn, shares)
    assert run(conn, provider, proposal_id).status == "executed"
    return fee_id


def collection_rows(conn) -> list:
    return conn.execute(
        "SELECT c.*, s.member_id FROM collections c JOIN shares s ON s.id = c.share_id ORDER BY c.rowid"
    ).fetchall()


def collection_of(conn, member_id: str):
    return next(r for r in collection_rows(conn) if r["member_id"] == member_id)


def texts(conn, member_id: str) -> list[str]:
    return [r["text"] for r in conn.execute("SELECT text FROM outbox WHERE member_id = ? ORDER BY id", (member_id,))]


def events(conn, name: str, family_id: str = FAM) -> list[dict]:
    return [e for e in audit.events(conn, family_id) if e["event"] == name]


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- asking for a share ------------------------------------------------------------------------


def test_a_split_asks_each_relative_for_a_payment_link(conn, provider):
    # Catches: a split that creates the shares but never sends a link, so nobody can pay.
    run_split(conn, provider)
    rows = collection_rows(conn)
    assert [(r["member_id"], r["status"]) for r in rows] == [
        ("m_baraka", "pending"),
        ("m_nakato", "pending"),
        ("m_amina", "pending"),
    ]
    for row in rows:
        mine = [t for t in texts(conn, row["member_id"]) if row["link"] in t]
        assert len(mine) == 1, row["member_id"]


def test_a_payment_link_goes_only_to_the_person_who_pays(conn, provider):
    # Catches: one relative being sent another's link, and so paying (or seeing) someone else's share.
    run_split(conn, provider)
    people = ("m_baraka", "m_nakato", "m_amina", "m_jabali")
    for row in collection_rows(conn):
        elsewhere = [t for m in people if m != row["member_id"] for t in texts(conn, m)]
        assert not any(row["link"] in t for t in elsewhere), row["member_id"]


def test_the_link_message_says_who_what_how_much_and_how(conn, provider):
    # Catches: a link message that doesn't say which fee it is for, what to pay, or by which method.
    run_split(conn, provider)
    row = collection_of(conn, "m_nakato")
    local = provider.quote("UGX", gbp("2000.00")).source
    (message,) = [t for t in texts(conn, "m_nakato") if row["link"] in t]
    assert message == (
        f"Hi Nakato, your share of the University of Westbridge fee is GBP 2,000.00. "
        f"Pay {local} by MTN Mobile Money here: {row['link']}"
    )


def test_each_country_pays_by_its_own_method_in_its_own_currency(conn, provider):
    # Catches: a Ugandan relative sent a Kenyan M-Pesa request, or asked for shillings of the wrong country.
    recording = Recording(provider, conn)
    run_split(conn, recording)
    by_payer = {c["payer_name"]: c for c in recording.calls}
    assert (by_payer["Baraka"]["method"], by_payer["Baraka"]["amount"].currency) == ("mpesa", "KES")
    assert (by_payer["Nakato"]["method"], by_payer["Nakato"]["amount"].currency) == ("mtn_ug", "UGX")


def test_a_payer_in_a_country_with_no_collect_method_gets_a_clear_error(conn, provider):
    # Catches: an IndexError from an empty list when a payer lives where Karo can send to but cannot collect yet.
    conn.execute("UPDATE members SET country = 'CM' WHERE id = 'm_baraka'")
    share_id = add_share(conn, new_fee(conn), "m_baraka", "100.00")
    with pytest.raises(ValueError, match="Cameroon"):
        request(conn, provider, share_id)


def test_the_provider_is_given_the_payers_details_and_the_return_page(conn, provider):
    # Catches: a collection with no payer contact, or one that sends the payer to the wrong page afterwards.
    recording = Recording(provider, conn)
    run_split(conn, recording)
    by_payer = {c["payer_name"]: c for c in recording.calls}
    assert by_payer["Baraka"]["payer_phone"] == "+254700000102"
    assert by_payer["Baraka"]["payer_email"] == "m_baraka@karo.example"  # he has no email on file
    assert by_payer["Amina"]["payer_email"] == "amina@example.com"
    assert {c["redirect_url"] for c in recording.calls} == {f"{BASE_URL}/paid"}


def test_the_row_holds_what_is_paid_locally_and_what_it_credits(conn, provider):
    # Catches: the local and credited amounts swapped, or the credit saved in the local currency.
    run_split(conn, provider)
    row = collection_of(conn, "m_nakato")
    local = provider.quote("UGX", gbp("2000.00")).source
    assert (row["amount_minor"], row["currency"]) == (local.minor, "UGX")
    assert (row["credit_minor"], row["credit_currency"]) == (gbp("2000.00").minor, "GBP")
    assert row["tx_ref"] == "karo-" + row["id"]
    assert row["provider"] == "standin" and row["link"].endswith(f"/standin/pay/{row['tx_ref']}")


def test_the_audit_log_records_each_request_but_never_the_link(conn, provider):
    # Catches: a payment link (which lets anyone pay that share) copied into the audit log.
    run_split(conn, provider)
    logged = events(conn, "collection.requested")
    assert len(logged) == 3
    assert {e["data"]["tx_ref"] for e in logged} == {r["tx_ref"] for r in collection_rows(conn)}
    everything = json.dumps(audit.events(conn, FAM))
    assert not any(r["link"] in everything for r in collection_rows(conn))


def test_a_share_is_asked_for_what_is_left_of_it(conn, provider):
    # Catches: asking again for the whole share after part of it was paid, which the relative would pay twice.
    share_id = add_share(conn, new_fee(conn), "m_nakato", "2000.00", paid="500.00")
    collection_id = request(conn, provider, share_id)
    row = conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
    assert (row["credit_minor"], row["credit_currency"]) == (gbp("1500.00").minor, "GBP")
    assert row["amount_minor"] == provider.quote("UGX", gbp("1500.00")).source.minor


def test_a_share_that_is_fully_paid_is_not_asked_for_again(conn, provider):
    # Catches: a zero-amount collection (or a crash building one) for a share with nothing left to pay.
    recording = Recording(provider, conn)
    share_id = add_share(conn, new_fee(conn), "m_nakato", "2000.00", paid="2000.00")
    assert request(conn, recording, share_id) is None
    assert recording.calls == [] and count(conn, "collections") == 0


def test_freezing_blocks_new_collection_requests(conn, provider):
    # Catches: Karo asking relatives to send money while all money movement is frozen.
    recording = Recording(provider, conn)
    share_id = add_share(conn, new_fee(conn), "m_nakato", "2000.00")
    conn.execute("UPDATE families SET frozen = 1 WHERE id = ?", (FAM,))
    assert request(conn, recording, share_id) is None
    assert recording.calls == [] and count(conn, "collections") == 0
    (blocked,) = events(conn, "collection.blocked")
    assert blocked["data"]["share_id"] == share_id and "frozen" in blocked["data"]["reason"]


def test_another_familys_share_is_not_collected(conn, provider):
    # Catches: a share id from one family used to start a collection (and message a member) in another.
    recording = Recording(provider, conn)
    share_id = add_share(conn, new_fee(conn), "m_nakato", "2000.00")
    assert request(conn, recording, share_id, family_id=OTHER) is None
    assert recording.calls == [] and count(conn, "collections") == 0
    assert texts(conn, "m_nakato") == []
    assert events(conn, "collection.blocked", OTHER) and not events(conn, "collection.blocked", FAM)


def test_the_provider_is_never_called_inside_a_transaction(conn, provider):
    # Catches: a slow provider call made while the write lock is held, which stops every other request.
    with db.transaction(conn):
        with pytest.raises(RuntimeError, match="transaction"):
            request(conn, provider, add_share(conn, new_fee(conn), "m_nakato", "2000.00"))


def test_request_all_asks_only_for_shares_still_owing(conn, provider):
    # Catches: a second round of requests also going to people who have already paid.
    fee_id = new_fee(conn)
    add_share(conn, fee_id, "m_baraka", "3000.00", paid="3000.00")
    add_share(conn, fee_id, "m_nakato", "2000.00")
    add_share(conn, fee_id, "m_amina", "1000.00", paid="400.00")
    asked = collections.request_all(conn, provider, family_id=FAM, fee_id=fee_id, now=NOW, base_url=BASE_URL)
    assert asked == 2
    assert {r["member_id"] for r in collection_rows(conn)} == {"m_nakato", "m_amina"}


def test_request_all_for_another_familys_fee_asks_nobody(conn, provider):
    # Catches: a fee id from another family pulling that family's relatives into this one's collection.
    fee_id = new_fee(conn)
    add_share(conn, fee_id, "m_nakato", "2000.00")
    asked = collections.request_all(conn, provider, family_id=OTHER, fee_id=fee_id, now=NOW, base_url=BASE_URL)
    assert asked == 0 and count(conn, "collections") == 0


# --- the executor's split ----------------------------------------------------------------------


def test_the_shares_are_committed_before_any_provider_call(conn, provider):
    # Catches: asking the provider for links inside the split's transaction, which holds the write lock
    # during the network call and loses the shares if a call fails.
    path = conn.execute("PRAGMA database_list").fetchone()["file"]

    class Watching(Recording):
        def create_collection(self, **kwargs):
            other = db.connect(path)
            try:
                self.shares_seen = other.execute("SELECT COUNT(*) FROM shares").fetchone()[0]
                other.execute("BEGIN IMMEDIATE")  # waits 5 s, then fails, if the writer still holds its lock
                other.execute("ROLLBACK")
            finally:
                other.close()
            return super().create_collection(**kwargs)

    watching = Watching(provider, conn)
    run_split(conn, watching)
    assert watching.shares_seen == 3
    assert watching.in_transaction == [False, False, False]


def test_a_split_stopped_at_the_executor_asks_for_no_links(conn, provider):
    # Catches: payment links going out for a split the rules refused when it came to run.
    recording = Recording(provider, conn)
    _, proposal_id = confirmed_split(conn)
    conn.execute("UPDATE families SET frozen = 1 WHERE id = ?", (FAM,))
    assert run(conn, recording, proposal_id).status == "blocked"
    assert recording.calls == [] and count(conn, "collections") == 0 and count(conn, "shares") == 0


def test_running_a_split_twice_asks_for_the_links_once(conn, provider):
    # Catches: a repeated execute (a double click on the PIN page) sending every relative a second link.
    _, proposal_id = confirmed_split(conn)
    assert run(conn, provider, proposal_id).status == "executed"
    assert run(conn, provider, proposal_id).status == "not_confirmed"
    assert count(conn, "collections") == 3


# --- a share that has no working link ----------------------------------------------------------


def live_links(conn, share_id: str) -> list:
    return conn.execute("SELECT * FROM collections WHERE share_id = ? AND status = 'pending'", (share_id,)).fetchall()


def share_of(conn, member_id: str) -> str:
    return conn.execute("SELECT id FROM shares WHERE member_id = ?", (member_id,)).fetchone()[0]


def test_a_split_where_one_link_request_fails_still_counts_and_says_so(conn, provider):
    # Catches: a split that is saved but whose result says "being processed" (or crashes) when links never went out.
    flaky = Flaky(provider, conn, fail_on=2)
    _, proposal_id = confirmed_split(conn)
    outcome = run(conn, flaky, proposal_id)
    assert outcome.status == "executed" and outcome.message.endswith(LINKS_LATER)
    assert "processed" not in outcome.message
    assert count(conn, "shares") == 3 and count(conn, "collections") == 1
    assert texts(conn, "m_amina")[-1] == LINKS_LATER
    (logged,) = events(conn, "collection.error")
    assert logged["data"]["error"] == "ConnectionError"


def test_reminders_give_a_share_that_has_no_link_a_working_one(conn, provider):
    # Catches: a share stranded for good after a failed link request, because reminders skipped it.
    fee_id, proposal_id = confirmed_split(conn)
    run(conn, Flaky(provider, conn, fail_on=2), proposal_id)
    assert live_links(conn, share_of(conn, "m_nakato")) == []
    assert remind(conn, provider, fee_id) == 3
    for member_id in ("m_nakato", "m_amina"):
        (link,) = live_links(conn, share_of(conn, member_id))
        assert provider.verify_collection(link["tx_ref"]).status == "pending"
        assert [t for t in texts(conn, member_id) if link["link"] in t]
    assert not any(t.startswith("Reminder") for m in ("m_nakato", "m_amina") for t in texts(conn, m))


def test_asking_again_asks_only_the_shares_without_a_live_link(conn, provider):
    # Catches: a retry that sends a second link to everyone who already has one.
    fee_id, proposal_id = confirmed_split(conn)
    run(conn, Flaky(provider, conn, fail_on=2), proposal_id)
    asked = collections.request_all(conn, provider, family_id=FAM, fee_id=fee_id, now=NOW, base_url=BASE_URL)
    assert asked == 2
    assert [len(live_links(conn, share_of(conn, m))) for m in ("m_baraka", "m_nakato", "m_amina")] == [1, 1, 1]
    assert collections.request_all(conn, provider, family_id=FAM, fee_id=fee_id, now=NOW, base_url=BASE_URL) == 0
    assert count(conn, "collections") == 3


def test_a_link_the_provider_has_lost_is_replaced_and_the_old_one_failed(conn, provider):
    # Catches: a share left holding a dead link after a restart, or holding two live links that could both be paid.
    share_id = add_share(conn, new_fee(conn), "m_nakato", "2000.00")
    old_id = request(conn, provider, share_id)
    new_id = request(conn, restarted(), share_id)
    assert new_id is not None and new_id != old_id
    status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM collections")}
    assert status == {old_id: "failed", new_id: "pending"}
    assert [e["data"]["collection_id"] for e in events(conn, "collection.failed")] == [old_id]


def test_a_link_the_provider_still_knows_is_not_replaced(conn, provider):
    # Catches: asking twice giving a share two live links, so it could be paid twice.
    recording = Recording(provider, conn)
    share_id = add_share(conn, new_fee(conn), "m_nakato", "2000.00")
    first = request(conn, recording, share_id)
    assert request(conn, recording, share_id) is None
    assert len(recording.calls) == 1 and count(conn, "collections") == 1
    assert conn.execute("SELECT status FROM collections WHERE id = ?", (first,)).fetchone()[0] == "pending"


def test_a_link_whose_payment_is_not_yet_booked_is_not_replaced(conn, provider):
    # Catches: replacing a link that has just been paid (the notice not arrived yet) and asking the payer to pay again.
    share_id = add_share(conn, new_fee(conn), "m_nakato", "2000.00")
    collection_id = request(conn, provider, share_id)
    provider.mark_paid(conn.execute("SELECT tx_ref FROM collections").fetchone()[0])
    assert request(conn, provider, share_id) is None
    assert conn.execute("SELECT status FROM collections WHERE id = ?", (collection_id,)).fetchone()[0] == "pending"


# --- reminders ---------------------------------------------------------------------------------


def remind(conn, provider, fee_id: str, family_id: str = FAM, now: datetime = NOW, **kw) -> int:
    return collections.send_reminders(
        conn, provider, family_id=family_id, fee_id=fee_id, now=now, base_url=BASE_URL, **kw
    )


def test_reminders_go_only_to_members_who_still_owe(conn, provider):
    # Catches: a reminder to someone who has already paid, which is how a family falls out.
    fee_id = run_split(conn, provider)
    conn.execute("UPDATE shares SET paid_minor = amount_minor WHERE member_id = 'm_baraka'")
    before = {m: len(texts(conn, m)) for m in ("m_baraka", "m_nakato", "m_amina")}
    assert remind(conn, provider, fee_id) == 2
    assert len(texts(conn, "m_baraka")) == before["m_baraka"]
    assert len(texts(conn, "m_nakato")) == before["m_nakato"] + 1
    assert len(texts(conn, "m_amina")) == before["m_amina"] + 1


def test_a_reminder_can_be_limited_to_one_share(conn, provider):
    # Catches: a per-share reminder that messages everyone who owes on the fee, so the scheduler pings them all.
    fee_id = run_split(conn, provider)
    nakato = collection_of(conn, "m_nakato")["share_id"]
    before = {m: len(texts(conn, m)) for m in ("m_baraka", "m_nakato", "m_amina")}
    assert remind(conn, provider, fee_id, share_id=nakato) == 1
    assert len(texts(conn, "m_nakato")) == before["m_nakato"] + 1
    assert len(texts(conn, "m_baraka")) == before["m_baraka"]
    assert len(texts(conn, "m_amina")) == before["m_amina"]
    assert [e["data"]["share_id"] for e in events(conn, "reminder.sent")] == [nakato]


def test_a_reminder_says_what_is_left_and_carries_the_shares_link(conn, provider):
    # Catches: a reminder for the full share after part-payment, or one without the link to pay with.
    fee_id = new_fee(conn)
    share_id = add_share(conn, fee_id, "m_nakato", "2000.00", paid="500.00")
    collection_id = request(conn, provider, share_id)
    link = conn.execute("SELECT link FROM collections WHERE id = ?", (collection_id,)).fetchone()[0]
    assert remind(conn, provider, fee_id) == 1
    assert texts(conn, "m_nakato")[-1] == (
        f"Reminder: your share GBP 1,500.00 for University of Westbridge is still due. Pay here: {link}"
    )


def test_a_dead_link_is_replaced_before_the_reminder_not_sent_again(conn, provider):
    # Catches: a reminder that re-sends a link the provider no longer has, which can never be paid.
    fee_id = new_fee(conn)
    share_id = add_share(conn, fee_id, "m_nakato", "2000.00")
    old_id = request(conn, provider, share_id)
    old_link = conn.execute("SELECT link FROM collections WHERE id = ?", (old_id,)).fetchone()[0]
    assert remind(conn, restarted(), fee_id) == 1
    (fresh,) = live_links(conn, share_id)
    assert fresh["id"] != old_id and fresh["link"] in texts(conn, "m_nakato")[-1]
    assert not any(old_link in t for t in texts(conn, "m_nakato")[1:])
    assert not any(t.startswith("Reminder") for t in texts(conn, "m_nakato"))


def test_a_failed_collection_is_not_sent_as_a_reminder(conn, provider):
    # Catches: a reminder carrying a link already marked failed.
    fee_id = new_fee(conn)
    share_id = add_share(conn, fee_id, "m_nakato", "2000.00")
    request(conn, provider, share_id)
    conn.execute("UPDATE collections SET status = 'failed'")
    assert remind(conn, provider, fee_id) == 1
    (fresh,) = live_links(conn, share_id)
    assert fresh["link"] in texts(conn, "m_nakato")[-1]


def test_a_member_with_no_link_yet_is_sent_one(conn, provider):
    # Catches: a share with no link at all getting silence, so it can never be paid.
    fee_id = new_fee(conn)
    share_id = add_share(conn, fee_id, "m_nakato", "2000.00")
    assert remind(conn, provider, fee_id) == 1
    (link,) = live_links(conn, share_id)
    assert link["link"] in texts(conn, "m_nakato")[-1]


def test_a_frozen_family_is_sent_no_new_links_but_old_ones_are_still_reminded(conn, provider):
    # Catches: asking relatives to send money while all money movement is frozen, just because it came as a reminder.
    fee_id = new_fee(conn)
    with_link = add_share(conn, fee_id, "m_baraka", "3000.00")
    request(conn, provider, with_link)
    add_share(conn, fee_id, "m_nakato", "2000.00")
    conn.execute("UPDATE families SET frozen = 1 WHERE id = ?", (FAM,))
    assert remind(conn, provider, fee_id) == 1
    assert texts(conn, "m_nakato") == [] and count(conn, "collections") == 1


def test_reminders_for_another_familys_fee_go_nowhere(conn, provider):
    # Catches: one family's fee id being used to message another family's relatives.
    fee_id = run_split(conn, provider)
    before = count(conn, "outbox")
    assert remind(conn, provider, fee_id, family_id=OTHER) == 0
    assert count(conn, "outbox") == before


def test_a_share_is_reminded_at_most_once_a_day_whoever_asks(conn, provider):
    # Catches: the AI's reminder tool messaging the same relatives again and again in one day.
    fee_id = run_split(conn, provider)
    assert remind(conn, provider, fee_id) == 3
    messages, audited = count(conn, "outbox"), len(events(conn, "reminder.sent"))
    assert remind(conn, provider, fee_id) == 0
    assert (count(conn, "outbox"), len(events(conn, "reminder.sent"))) == (messages, audited)
    assert remind(conn, provider, fee_id, now=NOW + timedelta(hours=24)) == 3
    assert len(events(conn, "reminder.sent")) == audited + 3


def test_each_reminder_is_audited_with_who_asked(conn, provider):
    # Catches: reminders sent with no record of who asked for them or who received them.
    fee_id = run_split(conn, provider)
    assert remind(conn, provider, fee_id, actor="m_amina") == 3
    logged = events(conn, "reminder.sent")
    assert sorted(e["data"]["member_id"] for e in logged) == ["m_amina", "m_baraka", "m_nakato"]
    assert {e["actor"] for e in logged} == {"m_amina"}
    assert not any(r["link"] in json.dumps(logged) for r in collection_rows(conn))


def test_reminders_refuse_to_run_inside_a_transaction(conn, provider):
    # Catches: reminders that can re-issue links (provider calls) while holding the write lock.
    fee_id = run_split(conn, provider)
    with db.transaction(conn):
        with pytest.raises(RuntimeError, match="transaction"):
            remind(conn, provider, fee_id)
