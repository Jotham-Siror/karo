"""Provider notices: believed only after the signature is checked and the payment re-checked with the provider.
Every test names the bug it would catch."""

import hashlib
import hmac
import json
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from karo import audit, collections, db, ledger, notices, seed, store
from karo.money import Money
from karo.providers.standin import SIGNATURE_HEADER, StandInProvider

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam_otieno"
OTHER = "fam_mwangi"
PIN = "4821"
PEPPER = "test-pepper"
SECRET = "standin-test-secret"
BASE_URL = "https://karo.example"
ALL_IN = "All shares are in. Ask me to pay the fees when you're ready."


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
    return StandInProvider.from_config(SECRET, BASE_URL)


class Wrapped:
    """The stand-in with one behaviour changed by a subclass. Everything else is passed through."""

    name = "standin"

    def __init__(self, inner: StandInProvider) -> None:
        self.inner = inner

    def __getattr__(self, attr):
        return getattr(self.inner, attr)


class Reports(Wrapped):
    """Answers every re-check with the status and amount chosen here, whatever was really paid."""

    def __init__(self, inner, *, status="succeeded", extra=0, currency=None) -> None:
        super().__init__(inner)
        self.status, self.extra, self.currency = status, extra, currency

    def verify_collection(self, tx_ref):
        real = self.inner.verify_collection(tx_ref)
        amount = Money(real.amount.minor + self.extra, self.currency or real.amount.currency)
        return replace(real, status=self.status, amount=amount)


class Down(Wrapped):
    """The first re-check fails like a dropped connection; later ones go through."""

    def __init__(self, inner) -> None:
        super().__init__(inner)
        self.checks = 0

    def verify_collection(self, tx_ref):
        self.checks += 1
        if self.checks == 1:
            raise ConnectionError("connection dropped")
        return self.inner.verify_collection(tx_ref)


class Overlapping(Wrapped):
    """While the first copy of a notice is being re-checked, a second copy of it arrives and is handled in full."""

    def __init__(self, inner, conn, headers, body) -> None:
        super().__init__(inner)
        self.conn, self.headers, self.body = conn, headers, body
        self.entered = False
        self.second_copy: str | None = None

    def verify_collection(self, tx_ref):
        if not self.entered:
            self.entered = True
            self.second_copy = notices.handle_notice(self.conn, self, headers=self.headers, body=self.body, now=NOW)
        return self.inner.verify_collection(tx_ref)


class Watching(Wrapped):
    """Remembers whether a transaction was open on `conn` at each provider call."""

    def __init__(self, inner, conn) -> None:
        super().__init__(inner)
        self.conn = conn
        self.open: dict[str, bool] = {}

    def verify_collection(self, tx_ref):
        self.open["verify_collection"] = self.conn.in_transaction
        return self.inner.verify_collection(tx_ref)

    def quote(self, source_currency, target):
        self.open["quote"] = self.conn.in_transaction
        return self.inner.quote(source_currency, target)


def new_fee(conn, family_id: str = FAM) -> str:
    state = store.load_state(conn, FAM, NOW)
    payee = next(t.payee for t in state.trusted_payees if t.id == "tp_westbridge")
    return store.insert_fee(
        conn, family_id=family_id, payee=payee, amount=gbp("6000.00"), due_date=None, invoice_ref="INV-77", now=NOW
    )


def add_share(conn, fee_id: str, member_id: str, amount: str, family_id: str = FAM) -> str:
    share_id = db.new_id("sh")
    conn.execute(
        "INSERT INTO shares (id, family_id, fee_id, member_id, amount_minor, currency, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'GBP', ?)",
        (share_id, family_id, fee_id, member_id, gbp(amount).minor, "2026-10-19T09:00:00+00:00"),
    )
    return share_id


def ask(conn, provider, share_id: str, family_id: str = FAM) -> sqlite3.Row:
    collection_id = collections.request_share(
        conn, provider, family_id=family_id, share_id=share_id, now=NOW, base_url=BASE_URL
    )
    return conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()


def one_share(conn, provider, member_id: str = "m_baraka", amount: str = "3000.00") -> sqlite3.Row:
    """A fee with a single share, asked for. Returns the collection."""
    return ask(conn, provider, add_share(conn, new_fee(conn), member_id, amount))


def sign(body: bytes, secret: str = SECRET) -> dict[str, str]:
    return {SIGNATURE_HEADER: hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}


def body_for(tx_ref: str | None, event_id: str | None = None) -> bytes:
    # The same bytes the stand-in's own mark_paid makes, so a test can send the notice before the payment.
    data = {"event_id": event_id or f"SI-EVT-{tx_ref}"}
    if tx_ref is not None:
        data["tx_ref"] = tx_ref
    return json.dumps(data, sort_keys=True).encode()


def handle(conn, provider, headers: dict, body: bytes, now: datetime = NOW) -> str:
    return notices.handle_notice(conn, provider, headers=headers, body=body, now=now)


def pay(conn, provider, collection: sqlite3.Row) -> str:
    """The payer pays, and the provider posts its notice."""
    headers, body = provider.mark_paid(collection["tx_ref"])
    return handle(conn, provider, headers, body)


def ledger_rows(conn, family_id: str = FAM) -> list[tuple]:
    rows = conn.execute(
        "SELECT currency, amount_minor, kind, ref FROM ledger WHERE family_id = ? ORDER BY seq", (family_id,)
    )
    return [tuple(r) for r in rows]


def paid_of(conn, share_id: str) -> int:
    return conn.execute("SELECT paid_minor FROM shares WHERE id = ?", (share_id,)).fetchone()[0]


def texts(conn, member_id: str) -> list[str]:
    return [r["text"] for r in conn.execute("SELECT text FROM outbox WHERE member_id = ? ORDER BY id", (member_id,))]


def events(conn, name: str, family_id: str | None = FAM) -> list[dict]:
    return [e for e in audit.events(conn, family_id) if e["event"] == name]


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def local_of(collection: sqlite3.Row) -> Money:
    return Money(collection["amount_minor"], collection["currency"])


# --- a paid notice ------------------------------------------------------------------------------


def test_a_confirmed_notice_credits_the_share_and_the_ledger(conn, provider):
    # Catches: a payment that arrives but never reaches the share or the family's balance.
    share_id = add_share(conn, new_fee(conn), "m_baraka", "3000.00")
    collection = ask(conn, provider, share_id)
    assert pay(conn, provider, collection) == "credited"
    row = conn.execute("SELECT * FROM collections WHERE id = ?", (collection["id"],)).fetchone()
    assert (row["status"], row["provider_id"]) == ("succeeded", f"SI-{collection['tx_ref']}")
    assert paid_of(conn, share_id) == gbp("3000.00").minor
    assert ledger_rows(conn) == [("KES", collection["amount_minor"], "collection", collection["id"])]


def test_a_share_paid_in_the_home_currency_is_not_converted(conn, provider):
    # Catches: a needless conversion (and fee-free profit or loss) on money that is already in the home currency.
    pay(conn, provider, one_share(conn, provider, "m_baraka"))
    assert [kind for _, _, kind, _ in ledger_rows(conn)] == ["collection"]
    assert not events(conn, "conversion.auto")


def test_a_ugx_share_credits_and_converts_to_kes(conn, provider):
    # Catches: Ugandan shillings left sitting in the ledger, where the family can't pay a KES-funded fee from them.
    collection = one_share(conn, provider, "m_nakato", "2000.00")
    assert pay(conn, provider, collection) == "credited"
    home = provider.quote("KES", gbp("2000.00")).source
    local = local_of(collection)
    assert ledger_rows(conn) == [
        ("UGX", local.minor, "collection", collection["id"]),
        ("UGX", -local.minor, "conversion", collection["id"]),
        ("KES", home.minor, "conversion", collection["id"]),
    ]
    assert ledger.balances(conn, FAM) == (home,)
    (auto,) = events(conn, "conversion.auto")
    assert auto["data"]["simulated"] is True
    assert auto["data"]["from"] == str(local) and auto["data"]["to"] == str(home)


def test_the_family_that_owns_the_collection_gets_the_credit(conn, provider):
    # Catches: a notice found by tx_ref (which names no family) crediting or messaging the wrong family.
    share_id = add_share(conn, new_fee(conn, OTHER), "m_wanjiru", "500.00", family_id=OTHER)
    collection = ask(conn, provider, share_id, family_id=OTHER)
    assert pay(conn, provider, collection) == "credited"
    assert ledger_rows(conn, OTHER) and not ledger_rows(conn, FAM)
    assert texts(conn, "m_wanjiru")[-1].startswith("Wanjiru has paid their share") and not texts(conn, "m_amina")


def test_the_main_payer_is_told_when_a_share_is_paid(conn, provider):
    # Catches: money arriving with nobody told, so the main payer keeps chasing a relative who has paid.
    fee_id = new_fee(conn)
    paid = ask(conn, provider, add_share(conn, fee_id, "m_baraka", "3000.00"))
    ask(conn, provider, add_share(conn, fee_id, "m_nakato", "3000.00"))
    pay(conn, provider, paid)
    assert texts(conn, "m_amina") == ["Baraka has paid their share (GBP 3,000.00)"]


def test_when_every_share_is_paid_the_main_payer_is_told_to_pay_the_fees(conn, provider):
    # Catches: a fully funded fee that nobody is told about, so it sits unpaid until the deadline.
    fee_id = new_fee(conn)
    first = ask(conn, provider, add_share(conn, fee_id, "m_baraka", "3000.00"))
    second = ask(conn, provider, add_share(conn, fee_id, "m_amina", "3000.00"))
    pay(conn, provider, first)
    assert not any(ALL_IN in t for t in texts(conn, "m_amina"))
    pay(conn, provider, second)
    assert texts(conn, "m_amina")[-1] == f"Amina has paid their share (GBP 3,000.00). {ALL_IN}"
    assert [e["data"]["fee_id"] for e in events(conn, "fee.collected")] == [fee_id]


def test_a_second_payment_on_a_paid_share_does_not_announce_the_fee_again(conn, provider):
    # Catches: "All shares are in" (and fee.collected) repeating when a stray extra payment lands, as it could if a
    # share ever held two live links (data from before the one-live-link rule).
    share_id = add_share(conn, new_fee(conn), "m_baraka", "3000.00")
    first = ask(conn, provider, share_id)
    conn.execute(
        "INSERT INTO collections SELECT 'col_clone', share_id, family_id, provider, 'karo-col_clone', link, "
        "amount_minor, currency, credit_minor, credit_currency, status, provider_id, created_at, updated_at "
        "FROM collections WHERE id = ?",
        (first["id"],),
    )
    provider.create_collection(
        tx_ref="karo-col_clone",
        amount=local_of(first),
        method="mpesa",
        payer_name="Baraka",
        payer_email="b@example.com",
        payer_phone=None,
        redirect_url=f"{BASE_URL}/paid",
    )
    second = conn.execute("SELECT * FROM collections WHERE id = 'col_clone'").fetchone()
    pay(conn, provider, first)
    assert pay(conn, provider, second) == "credited"
    assert sum(ALL_IN in t for t in texts(conn, "m_amina")) == 1
    assert len(events(conn, "fee.collected")) == 1


# --- a notice that is not to be believed --------------------------------------------------------


@pytest.mark.parametrize("how", ["wrong_secret", "no_signature", "body_changed"])
def test_a_forged_notice_is_rejected_and_nothing_is_credited(conn, provider, how):
    # Catches: anyone who can reach the webhook URL paying a share by posting a made-up notice.
    collection = one_share(conn, provider)
    body = body_for(collection["tx_ref"])
    headers = {
        "wrong_secret": sign(body, "not-the-secret"),
        "no_signature": {},
        "body_changed": sign(body_for("karo-col_some_other_one")),
    }[how]
    assert handle(conn, provider, headers, body) == "rejected"
    assert ledger_rows(conn) == [] and count(conn, "provider_events") == 0
    assert conn.execute("SELECT status FROM collections").fetchone()[0] == "pending"
    (rejected,) = events(conn, "notice.rejected", None)
    assert collection["tx_ref"] not in json.dumps(rejected)


def test_the_same_notice_twice_credits_once(conn, provider):
    # Catches: a provider retry (or a replayed notice) paying the same share twice.
    share_id = add_share(conn, new_fee(conn), "m_baraka", "3000.00")
    collection = ask(conn, provider, share_id)
    headers, body = provider.mark_paid(collection["tx_ref"])
    assert handle(conn, provider, headers, body) == "credited"
    assert handle(conn, provider, headers, body) == "duplicate"
    assert paid_of(conn, share_id) == gbp("3000.00").minor
    assert len(ledger_rows(conn)) == 1 and len(texts(conn, "m_amina")) == 1


def test_a_new_event_for_a_collection_already_credited_adds_nothing(conn, provider):
    # Catches: de-duplicating on the event id alone, so a re-sent payment under a fresh id is credited twice.
    share_id = add_share(conn, new_fee(conn), "m_baraka", "3000.00")
    collection = ask(conn, provider, share_id)
    pay(conn, provider, collection)
    body = body_for(collection["tx_ref"], event_id="SI-EVT-another")
    assert handle(conn, provider, sign(body), body) == "duplicate"
    assert paid_of(conn, share_id) == gbp("3000.00").minor and len(ledger_rows(conn)) == 1


def test_a_notice_the_provider_does_not_confirm_is_unverified(conn, provider):
    # Catches: believing a correctly signed notice without asking the provider whether the money really arrived.
    collection = one_share(conn, provider)
    body = body_for(collection["tx_ref"])  # signed, but the stand-in has not been paid
    assert handle(conn, provider, sign(body), body) == "unverified"
    assert ledger_rows(conn) == [] and conn.execute("SELECT status FROM collections").fetchone()[0] == "pending"
    (logged,) = events(conn, "collection.unverified")
    assert logged["data"]["tx_ref"] == collection["tx_ref"]


@pytest.mark.parametrize(
    ("reported", "outcome"),
    [
        (dict(status="pending"), "unverified"),
        (dict(status="failed"), "unverified"),
        (dict(extra=-1), "unverified"),
        (dict(currency="UGX"), "unverified"),
        (dict(extra=0), "credited"),
        (dict(extra=1), "credited"),
    ],
)
def test_the_recheck_must_show_success_in_the_same_currency_for_at_least_the_amount(conn, provider, reported, outcome):
    # Catches: crediting a share that the provider says is pending, failed, short, or paid in another currency.
    collection = one_share(conn, provider)
    headers, body = provider.mark_paid(collection["tx_ref"])
    assert handle(conn, Reports(provider, **reported), headers, body) == outcome
    assert bool(ledger_rows(conn)) == (outcome == "credited")


def test_a_notice_that_could_not_be_confirmed_can_be_sent_again_once_it_can(conn, provider):
    # Catches: a real payment lost for good because its first, unconfirmed notice used up the event id.
    collection = one_share(conn, provider)
    body = body_for(collection["tx_ref"])
    assert handle(conn, provider, sign(body), body) == "unverified"
    assert count(conn, "provider_events") == 0
    headers, same_body = provider.mark_paid(collection["tx_ref"])
    assert same_body == body
    assert handle(conn, provider, headers, same_body) == "credited"


def test_a_crash_during_the_recheck_leaves_no_trace_and_the_retry_credits_once(conn, provider):
    # Catches: the event id saved before the payment is confirmed, so a crash mid-check turns the provider's retry
    # into a "duplicate" and the payment is never credited.
    share_id = add_share(conn, new_fee(conn), "m_baraka", "3000.00")
    collection = ask(conn, provider, share_id)
    headers, body = provider.mark_paid(collection["tx_ref"])
    down = Down(provider)
    with pytest.raises(ConnectionError):
        handle(conn, down, headers, body)
    assert ledger_rows(conn) == [] and count(conn, "provider_events") == 0
    assert handle(conn, down, headers, body) == "credited"
    assert handle(conn, down, headers, body) == "duplicate"
    assert len(ledger_rows(conn)) == 1 and paid_of(conn, share_id) == gbp("3000.00").minor


def test_a_copy_that_arrives_during_the_recheck_cannot_credit_a_second_time(conn, provider):
    # Catches: two copies of one notice both passing the "seen it?" look before either has credited, and both paying.
    share_id = add_share(conn, new_fee(conn), "m_baraka", "3000.00")
    collection = ask(conn, provider, share_id)
    headers, body = provider.mark_paid(collection["tx_ref"])
    overlapping = Overlapping(provider, conn, headers, body)
    assert handle(conn, overlapping, headers, body) == "duplicate"
    assert overlapping.second_copy == "credited"
    assert len(ledger_rows(conn)) == 1 and paid_of(conn, share_id) == gbp("3000.00").minor
    assert len(texts(conn, "m_amina")) == 1 and count(conn, "provider_events") == 1


@pytest.mark.parametrize("tx_ref", ["karo-col_nobody", None])
def test_a_notice_for_a_payment_we_never_asked_for_is_unknown(conn, provider, tx_ref):
    # Catches: a validly signed notice for something unrelated crashing, or crediting an unrelated share.
    one_share(conn, provider)
    body = body_for(tx_ref)
    assert handle(conn, provider, sign(body), body) == "unknown"
    assert ledger_rows(conn) == []


def test_a_failure_part_way_leaves_nothing_half_credited(conn, provider):
    # Catches: the collection marked paid and the ledger credited but the share not (or the reverse), if a write fails.
    share_id = add_share(conn, new_fee(conn), "m_nakato", "2000.00")
    collection = ask(conn, provider, share_id)
    headers, body = provider.mark_paid(collection["tx_ref"])
    conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON audit WHEN NEW.event = 'conversion.auto' "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        handle(conn, provider, headers, body)
    assert conn.execute("SELECT status FROM collections").fetchone()[0] == "pending"
    assert ledger_rows(conn) == [] and paid_of(conn, share_id) == 0 and texts(conn, "m_amina") == []
    assert count(conn, "provider_events") == 0
    conn.execute("DROP TRIGGER boom")
    assert handle(conn, provider, headers, body) == "credited"


def test_the_provider_is_never_called_inside_a_transaction(conn, provider):
    # Catches: a re-check or a quote made while the write lock is held, which stops every other request.
    collection = one_share(conn, provider, "m_nakato", "2000.00")
    watching = Watching(provider, conn)
    headers, body = provider.mark_paid(collection["tx_ref"])
    assert handle(conn, watching, headers, body) == "credited"
    assert watching.open == {"verify_collection": False, "quote": False}
