"""The web door: login, chat, the PIN page, the log and the demo controls. Every test names the bug it would catch."""

import hashlib
import hmac
import html
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from karo import approvals, audit, clock, collections, db, ledger, outbox, proposals, quotes, seed, store
from karo.agent.claude import FakeClaude, text_response
from karo.config import Settings
from karo.domain import Kind, Payee
from karo.money import Money
from karo.providers.standin import SIGNATURE_HEADER, StandInProvider
from karo.web import app as web_app
from karo.web.app import HideConfirmTokens, create_app, run_scheduled_tick

FAM = "fam_otieno"
PIN = "4821"
PEPPER = "test-pepper"
PASSWORD = "demo-password-xyz"
SECRET = "standin-secret-xyz"
BASE_URL = "http://testserver"
EXPIRED = "This link has expired or was already used."
PROCESSING = "Your payment is being processed. You'll get a message when it's done."


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    # Seeding hashes two PINs with Argon2, which is deliberately slow: do it once and copy the file per test.
    path = tmp_path_factory.mktemp("template") / "karo.db"
    connection = db.connect(path)
    db.init(connection)
    seed.seed_demo(connection, pin=PIN, pepper=PEPPER, now=datetime.now(UTC))
    connection.close()
    return path


@pytest.fixture
def settings(seeded_db, tmp_path):
    target = tmp_path / "karo.db"
    shutil.copyfile(seeded_db, target)
    return Settings(
        db_path=str(target),
        public_base_url=BASE_URL,
        pin_pepper=PEPPER,
        session_secret="session-secret-xyz",
        demo_password=PASSWORD,
        standin_secret=SECRET,
        demo_pin=PIN,
    )


class DroppingProvider:
    """Quotes like the stand-in, but the connection drops when a payout is sent."""

    name = "standin"

    def __init__(self, inner: StandInProvider) -> None:
        self.inner = inner

    def quote(self, *args, **kwargs):
        return self.inner.quote(*args, **kwargs)

    def payout(self, **kwargs):
        raise ConnectionError("connection dropped")

    def payout_status(self, **kwargs):
        return self.inner.payout_status(**kwargs)


class Site:
    """The app under test, a browser that keeps its cookies, and the scripted AI behind it."""

    def __init__(self, settings: Settings, steps=(), provider=None) -> None:
        self.settings = settings
        self.claude = FakeClaude(steps)
        self.provider = provider or StandInProvider.from_config(SECRET, BASE_URL)
        app = create_app(settings, client=self.claude, provider=self.provider)
        self.client = TestClient(app, follow_redirects=False)

    @contextmanager
    def connect(self):
        # Always closed: the reset test deletes the database file, which Windows refuses while a connection is open.
        with closing(db.connect(self.settings.db_path)) as conn:
            yield conn

    def query(self, sql: str, *args):
        with self.connect() as conn:
            return conn.execute(sql, args).fetchall()

    def login(self, person: str = "amina"):
        response = self.client.post("/login", data={"password": PASSWORD, "person": person})
        assert response.status_code == 303
        return response

    def chat(self, text: str, **body):
        return self.client.post("/api/chat", json={"messages": [{"role": "user", "text": text}], **body})


def words(response) -> str:
    """The page as a person reads it: HTML escapes (an apostrophe is &#39;) turned back into characters."""
    return html.unescape(response.text)


@pytest.fixture
def make_site(settings):
    return lambda steps=(), provider=None: Site(settings, steps, provider)


@pytest.fixture
def site(make_site):
    return make_site()


# --- starting the app ----------------------------------------------------------------------------


def test_first_start_creates_and_seeds_the_database(tmp_path):
    # Catches: a first run on a clean checkout with no database, where nobody can log in.
    path = tmp_path / "fresh" / "karo.db"
    fresh = Settings(
        db_path=str(path),
        pin_pepper=PEPPER,
        session_secret="s",
        demo_password=PASSWORD,
        standin_secret=SECRET,
        demo_pin=PIN,
    )
    Site(fresh)
    with closing(db.connect(path)) as conn:
        assert [r["id"] for r in conn.execute("SELECT id FROM families ORDER BY id")] == ["fam_mwangi", "fam_otieno"]
        assert approvals.verify_pin(conn, FAM, "m_amina", PIN, PEPPER, clock.now(conn)).ok


def test_first_start_without_the_demo_pin_says_what_is_missing(tmp_path):
    # Catches: seeding a family with no PIN, so the first payment can never be confirmed.
    fresh = Settings(db_path=str(tmp_path / "karo.db"), pin_pepper=PEPPER, session_secret="s", demo_password=PASSWORD)
    with pytest.raises(RuntimeError, match="DEMO_PIN"):
        create_app(fresh, client=FakeClaude([]), provider=StandInProvider.from_config(SECRET, BASE_URL))


def test_a_restart_keeps_the_existing_database(settings):
    # Catches: every restart re-seeding or wiping the family's data.
    first = Site(settings)
    first.login()
    first.chat("stop")
    second = Site(settings)
    assert second.query("SELECT frozen FROM families WHERE id = ?", FAM)[0]["frozen"] == 1


def test_without_an_injected_client_the_api_key_is_required(settings):
    # Catches: starting the real app with no key, then failing on the first chat message.
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        create_app(settings, provider=StandInProvider.from_config(SECRET, BASE_URL))


def test_an_unknown_provider_is_refused_at_start(settings):
    # Catches: KARO_PROVIDER=flutterwave quietly running on the stand-in, which pays nothing.
    with pytest.raises(RuntimeError, match="flutterwave"):
        create_app(replace(settings, provider="flutterwave"), client=FakeClaude([]))


def test_importing_the_web_module_needs_no_environment():
    # Catches: building the app at import time, which makes the tests (and any tool that imports it) need real secrets.
    own = ("ANTHROPIC_API_KEY", "DEMO_PIN")
    env = {k: v for k, v in os.environ.items() if not k.startswith("KARO_") and k not in own}
    done = subprocess.run(
        [sys.executable, "-I", "-c", "import karo.web.app"], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr


def test_the_module_level_app_is_built_once_on_first_use(settings, monkeypatch):
    # Catches: `uvicorn karo.web.app:app` finding no app, or building a new one (and a new provider) per access.
    module = importlib.import_module("karo.web.app")
    monkeypatch.setattr(module, "load_settings", lambda: replace(settings, anthropic_api_key="sk-test-not-real"))
    monkeypatch.setattr(module, "_app", None)
    built = module.app
    assert isinstance(built, FastAPI)
    assert module.app is built


# --- logging in ----------------------------------------------------------------------------------


def test_the_login_page_asks_for_the_demo_password_and_lists_the_people(site):
    # Catches: a login page with no way to pick who you are, so the demo can't show each role.
    page = site.client.get("/")
    assert page.status_code == 200
    assert "Demo password" in page.text
    for name in ("Amina", "Baraka", "Nakato", "Jabali", "Wanjiru"):
        assert name in page.text


def test_the_chat_sends_a_logged_out_visitor_to_the_login_page(site):
    # Catches: the chat page opening for anyone who knows the address.
    response = site.client.get("/chat")
    assert (response.status_code, response.headers["location"]) == (303, "/")


def test_a_wrong_password_does_not_log_in(site):
    # Catches: a wrong password being accepted, or setting a session before the check.
    response = site.client.post("/login", data={"password": "nope", "person": "amina"})
    assert response.status_code == 401
    assert "isn't right" in words(response)
    assert site.client.get("/chat").status_code == 303


def test_a_person_who_is_not_in_the_list_cannot_be_picked(site):
    # Catches: any text being accepted as the person, putting a made-up address in the session.
    response = site.client.post("/login", data={"password": PASSWORD, "person": "nobody"})
    assert response.status_code == 400
    assert site.client.get("/chat").status_code == 303


def test_the_chat_page_says_who_you_are(site):
    # Catches: a relative being shown as the main payer, or a page that doesn't say who is logged in.
    site.login("baraka")
    page = site.client.get("/chat")
    assert page.status_code == 200
    assert "Baraka" in page.text and "relative" in page.text


def test_the_chat_page_loads_deep_chat_at_the_pinned_version(site):
    # Catches: an unpinned or moved CDN script, which would run whatever the CDN serves on the page that holds the chat.
    site.login()
    page = site.client.get("/chat").text
    assert "https://cdn.jsdelivr.net/npm/deep-chat@2.5.1/dist/deepChat.bundle.js" in page
    assert 'integrity="sha384-' in page
    assert "/api/chat" in page and "/api/inbox" in page


def storage_key(page: str) -> str:
    return json.loads(re.search(r"browserStorage='([^']*)'", page).group(1))["key"]


def test_the_chat_is_kept_in_the_browser_for_one_person_and_one_database(site):
    # Catches: a reload losing the confirm link (it is only delivered once), people sharing one saved chat,
    # or messages from before a demo reset coming back.
    site.login("amina")
    amina = storage_key(site.client.get("/chat").text)
    site.login("baraka")
    baraka = storage_key(site.client.get("/chat").text)
    site.client.post("/demo/reset")
    after_reset = storage_key(site.client.get("/chat").text)
    assert len({amina, baraka, after_reset}) == 3


def test_logging_out_ends_the_session(site):
    # Catches: logout leaving the cookie working.
    site.login()
    assert site.client.get("/logout").status_code == 303
    assert site.client.get("/chat").status_code == 303


def test_the_stylesheet_is_served(site):
    # Catches: pages with no styling because the static folder isn't mounted where the templates look.
    assert site.client.get("/static/style.css").status_code == 200


# --- the chat API --------------------------------------------------------------------------------


def test_chat_needs_a_login(site):
    # Catches: the agent being callable, and spending the API budget, without logging in.
    response = site.chat("hi")
    assert response.status_code == 401
    assert "error" in response.json()
    assert site.claude.requests == []


def test_chat_returns_the_agents_reply_to_the_logged_in_person(make_site):
    # Catches: the reply being lost, or the agent being told the wrong person is talking.
    site = make_site([text_response("Hello Baraka.")])
    site.login("baraka")
    response = site.chat("hi")
    assert response.json() == {"text": "Hello Baraka."}
    request = site.claude.requests[0]
    assert "You are talking to Baraka" in request["system"]
    assert request["messages"][-1]["content"] == [{"type": "text", "text": "hi"}]


def test_chat_reads_deep_chats_multipart_message_with_a_photo(make_site):
    # Catches: invoice photos being ignored because the multipart shape Deep Chat sends isn't read.
    site = make_site([text_response("I see the invoice.")])
    site.login()
    response = site.client.post(
        "/api/chat",
        data={"message1": json.dumps({"role": "user", "text": "Here is the invoice"}), "message_id": "client-id-0001"},
        files=[("files", ("invoice.png", b"\x89PNG-bytes", "image/png"))],
    )
    assert response.json() == {"text": "I see the invoice."}
    content = site.claude.requests[0]["messages"][-1]["content"]
    assert [block["type"] for block in content] == ["image", "text"]
    assert content[1]["text"] == "Here is the invoice"


def test_chat_accepts_a_photo_sent_with_no_text(make_site):
    # Catches: Deep Chat sending a file-only message with no message field, which then fails as "no message".
    site = make_site([text_response("I see the invoice.")])
    site.login()
    response = site.client.post("/api/chat", files=[("files", ("invoice.png", b"PNG-bytes", "image/png"))])
    assert response.json() == {"text": "I see the invoice."}
    assert [b["type"] for b in site.claude.requests[0]["messages"][-1]["content"]] == ["image"]


def test_chat_reads_the_newest_of_several_multipart_messages(make_site):
    # Catches: message10 sorting before message2, so an old message is sent as if it were the new one.
    site = make_site([text_response("ok")])
    site.login()
    data = {f"message{i}": json.dumps({"role": "user", "text": f"text {i}"}) for i in (1, 2, 10)}
    site.client.post("/api/chat", data=data, files=[("files", ("a.png", b"x", "image/png"))])
    assert site.claude.requests[0]["messages"][-1]["content"][-1]["text"] == "text 10"


@pytest.mark.parametrize("body", [b"not json", b'{"messages": []}', b'{"messages": "hi"}', b"[]"])
def test_chat_refuses_a_body_it_cannot_read(site, body):
    # Catches: a malformed request becoming a 500 or reaching the agent as an empty message.
    site.login()
    response = site.client.post("/api/chat", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert "error" in response.json()
    assert site.claude.requests == []


def test_the_same_client_message_id_runs_the_agent_once(make_site):
    # Catches: a retried request running the agent (and any money tools) twice for one message.
    site = make_site([text_response("First."), text_response("Second.")])
    site.login()
    first = site.chat("hi", message_id="client-id-0001")
    second = site.chat("hi", message_id="client-id-0001")
    assert first.json() == {"text": "First."}
    assert "already" in second.json()["text"]
    assert len(site.claude.requests) == 1


def test_one_persons_message_id_cannot_block_anothers(make_site):
    # Catches: a client-chosen id swallowing someone else's message that happens to use the same id.
    site = make_site([text_response("For Amina."), text_response("For Baraka.")])
    site.login("amina")
    site.chat("hi", message_id="client-id-0001")
    site.login("baraka")
    assert site.chat("hi", message_id="client-id-0001").json() == {"text": "For Baraka."}


def test_a_freeze_word_over_the_web_freezes_without_calling_the_agent(site):
    # Catches: the emergency stop depending on the AI, or being missed on the web door.
    site.login("baraka")
    response = site.chat("Stop")
    assert "frozen" in response.json()["text"].lower()
    assert site.query("SELECT frozen FROM families WHERE id = ?", FAM)[0]["frozen"] == 1
    assert site.claude.requests == []


def test_an_agent_failure_over_the_web_is_a_reply_not_a_500(make_site):
    # Catches: a model outage showing the person a server error instead of a plain sorry.
    def fail(_request):
        raise ConnectionError("model unreachable")

    site = make_site([fail])
    site.login()
    response = site.chat("hi")
    assert response.status_code == 200
    assert response.json() == {"text": "Sorry, something went wrong on my side. Please try again in a minute."}


def test_the_inbox_hands_over_waiting_messages_once(site):
    # Catches: the same message appearing in the chat every three seconds.
    site.login()
    with site.connect() as conn:
        outbox.notify(conn, "m_amina", "Your payment is ready.", clock.now(conn))
    assert site.client.get("/api/inbox").json() == [{"text": "Your payment is ready."}]
    assert site.client.get("/api/inbox").json() == []


def test_the_inbox_only_holds_your_own_messages(site):
    # Catches: one person's confirm link or receipt turning up in another person's chat.
    with site.connect() as conn:
        outbox.notify(conn, "m_amina", "Amina's private message.", clock.now(conn))
    site.login("baraka")
    assert site.client.get("/api/inbox").json() == []
    site.login("amina")
    assert [m["text"] for m in site.client.get("/api/inbox").json()] == ["Amina's private message."]


def test_the_inbox_needs_a_login(site):
    # Catches: the outbox, which holds confirm links, being readable without logging in.
    response = site.client.get("/api/inbox")
    assert response.status_code == 401


# --- the PIN page --------------------------------------------------------------------------------


def start(site, *, kind=Kind.PAY_FEE, payee: Payee | None = None, just_trusted: bool = False) -> str:
    """Has Amina's agent propose something that needs her PIN, and returns the token from her confirm link.

    A fee goes to the university, or with `just_trusted` to a payee trusted only a moment ago, which makes it wait."""
    with site.connect() as conn:
        now = clock.now(conn)
        amina = store.member(conn, FAM, "m_amina")
        if kind is Kind.PAY_FEE:
            ledger.post(
                conn, family_id=FAM, at=now, amount=Money.parse("5000000", "KES"), kind="collection", ref="test-funds"
            )
            if just_trusted:
                payee = Payee.of(
                    "New Payee Ltd", country="GB", currency="GBP", sort_code="11-22-33", account_number="12345678"
                )
                conn.execute(
                    "INSERT INTO trusted_payees (id, family_id, name, details_json, trusted_since, source) "
                    "VALUES ('tp_new', ?, ?, ?, ?, 'added')",
                    (FAM, payee.name, json.dumps(dict(payee.details)), clock.iso(now)),
                )
            else:
                payee = store.load_state(conn, FAM, now).trusted_payees[0].payee
            target = Money.parse("6000.00", "GBP")
            fee_id = store.insert_fee(
                conn, family_id=FAM, payee=payee, amount=target, due_date=None, invoice_ref="INV-77", now=now
            )
            quote = quotes.create_quote(
                conn, site.provider, family_id=FAM, source_currency="KES", target=target, now=now
            )
            fields = {"amount": target, "quote_id": quote.id, "payee": payee, "fee_id": fee_id}
        else:
            fields = {"payee": payee}
        outcome = proposals.propose(
            conn, family_id=FAM, actor=amina, kind=kind, now=now, base_url=BASE_URL, **fields
        )
        assert outcome.status == "awaiting_pin"
        row = conn.execute(
            "SELECT text FROM outbox WHERE member_id = 'm_amina' AND text LIKE '%/confirm/%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return re.search(r"/confirm/(\S+)", row["text"]).group(1)


def enter(site, token, pin=PIN):
    return site.client.post(f"/confirm/{token}", data={"pin": pin})


def status_of_only_proposal(site) -> str:
    return site.query("SELECT status FROM proposals")[0]["status"]


def test_the_pin_page_shows_the_payment_the_payee_and_a_numeric_pin_field(site):
    # Catches: a PIN page that doesn't say what the PIN approves, or that lets the browser remember the PIN.
    token = start(site)
    page = site.client.get(f"/confirm/{token}")
    assert page.status_code == 200
    assert "Pay GBP 6,000.00 to University of Westbridge" in page.text
    for detail in ("20-40-60", "41234567", "Sort code", "Account number"):
        assert detail in page.text
    assert 'inputmode="numeric"' in page.text and 'autocomplete="off"' in page.text


def test_the_pin_page_puts_what_needs_attention_ahead_of_the_checks_that_passed(site):
    # Catches: a PIN page that buries a rule that is holding the payment under a long list of passes.
    token = start(site, just_trusted=True)
    text = words(site.client.get(f"/confirm/{token}"))
    assert "Has to wait. New Payee Ltd is a new payee." in text
    assert text.index("Has to wait") < text.index("9 checks passed")


class Collected(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def test_the_access_log_does_not_carry_the_confirm_token(site):
    # Catches: uvicorn's access log printing the one-time confirm token, which undoes storing only its hash.
    access = logging.getLogger("uvicorn.access")
    collected = Collected()
    access.addHandler(collected)
    try:
        # The arguments uvicorn gives its access log: client, method, path with query, HTTP version, status.
        for method in ("GET", "POST"):
            path = "/confirm/abc123_-XYZ?x=1"
            access.warning('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", method, path, "1.1", 200)
    finally:
        access.removeHandler(collected)
    expected = ['127.0.0.1:5000 - "%s /confirm/<redacted>?x=1 HTTP/1.1" 200' % method for method in ("GET", "POST")]
    assert collected.lines == expected


def test_making_the_app_twice_installs_one_token_filter(make_site):
    # Catches: every create_app (each test, each reload) stacking another filter on the access log.
    make_site()
    make_site()
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, HideConfirmTokens) for f in access.filters) == 1


def test_the_pin_page_is_not_cached_or_framed_and_leaks_no_referrer(site):
    # Catches: the one-time link page being cached, framed by another site, or sent on in a Referer header.
    token = start(site)
    headers = site.client.get(f"/confirm/{token}").headers
    assert headers["cache-control"] == "no-store"
    assert headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


def test_the_pin_page_opens_without_logging_in(site):
    # Catches: the main payer, who arrives from a WhatsApp link with no web session, being sent to the login page.
    token = start(site)
    assert site.client.get(f"/confirm/{token}").status_code == 200


def test_an_unknown_link_says_it_has_expired(site):
    # Catches: a guessed or old link showing a stack trace, or a payment, instead of a plain message.
    response = site.client.get("/confirm/not-a-real-token")
    assert response.status_code == 404
    assert EXPIRED in response.text


def test_a_wrong_pin_says_how_many_attempts_are_left(site):
    # Catches: a wrong PIN giving no feedback, or the count not following the family's own limit.
    token = start(site)
    first = enter(site, token, "0000")
    assert first.status_code == 403
    assert "2 attempts left" in first.text
    assert "1 attempt left" in enter(site, token, "0000").text
    assert status_of_only_proposal(site) == "awaiting_pin"


def test_three_wrong_pins_lock_the_page_and_say_until_when(site):
    # Catches: unlimited PIN guessing, or a lockout that doesn't say when to come back.
    token = start(site)
    enter(site, token, "0000")
    enter(site, token, "0000")
    locked = enter(site, token, "0000")
    assert locked.status_code == 429
    assert re.search(r"Try again after \w{3} \d{2} \w{3}, \d{2}:\d{2} \w+", locked.text)
    assert enter(site, token, PIN).status_code == 429
    assert status_of_only_proposal(site) == "awaiting_pin"
    assert "Try again after" in site.client.get(f"/confirm/{token}").text


def test_a_pin_that_is_not_four_to_six_digits_is_not_counted_as_a_guess(site):
    # Catches: a typo like "12" using up one of the three attempts.
    token = start(site)
    response = enter(site, token, "12")
    assert response.status_code == 400
    assert "4 to 6 digits" in response.text
    assert site.query("SELECT pin_failures FROM families WHERE id = ?", FAM)[0]["pin_failures"] == 0


def test_the_right_pin_pays_and_shows_the_receipt(site):
    # Catches: a confirmed payment that is never executed, or a result page with no receipt.
    token = start(site)
    response = enter(site, token)
    assert response.status_code == 200
    assert "Paid GBP 6,000.00 to University of Westbridge" in response.text
    assert "simulated" in response.text
    assert status_of_only_proposal(site) == "executed"
    assert [r["status"] for r in site.query("SELECT status FROM payouts")] == ["succeeded"]


def test_a_payment_that_has_to_wait_says_so_after_the_pin(site):
    # Catches: a held payment being shown as paid, or the PIN being refused for a payment that is only held.
    token = start(site, just_trusted=True)
    response = enter(site, token)
    assert response.status_code == 200
    assert "<h1>Waiting</h1>" in response.text and "It can run after" in response.text
    assert "Paid" not in response.text
    assert site.query("SELECT id FROM payouts") == []


def test_a_link_cannot_be_used_twice(site):
    # Catches: replaying a used link to pay the same invoice again.
    token = start(site)
    enter(site, token)
    again = enter(site, token)
    assert again.status_code == 404
    assert EXPIRED in again.text
    assert EXPIRED in site.client.get(f"/confirm/{token}").text
    assert len(site.query("SELECT id FROM payouts")) == 1


def test_a_provider_error_after_the_pin_says_the_payment_is_being_processed(make_site):
    # Catches: a provider outage after a correct PIN becoming a 500 page, which looks like the payment failed.
    inner = StandInProvider.from_config(SECRET, BASE_URL)
    site = make_site(provider=DroppingProvider(inner))
    token = start(site)
    response = enter(site, token)
    assert response.status_code == 200
    assert PROCESSING in words(response)
    assert status_of_only_proposal(site) == "confirmed"
    assert [r["status"] for r in site.query("SELECT status FROM payouts")] == ["pending"]
    with site.connect() as conn:
        logged = [e for e in audit.events(conn, FAM) if e["event"] == "execute.error"]
    assert [e["data"]["error"] for e in logged] == ["ConnectionError"]


def test_a_link_for_a_proposal_that_is_no_longer_waiting_says_so(site):
    # Catches: a PIN being accepted for a proposal that has already moved on.
    token = start(site)
    with site.connect() as conn:
        conn.execute("UPDATE proposals SET status = 'blocked'")
    page = site.client.get(f"/confirm/{token}")
    assert page.status_code == 409 and "no longer waiting" in page.text
    assert enter(site, token).status_code == 409
    assert site.query("SELECT COUNT(*) AS n FROM confirmations")[0]["n"] == 0


def test_a_script_in_a_payee_name_is_shown_as_text_on_the_pin_page(site):
    # Catches: a payee name from an invoice running as code on the page where the PIN is typed.
    evil = Payee.of(
        "<script>alert(1)</script>", country="GB", currency="GBP", sort_code="11-22-33", account_number="12345678"
    )
    token = start(site, kind=Kind.ADD_PAYEE, payee=evil)
    page = site.client.get(f"/confirm/{token}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)" not in page


# --- the log and the demo controls ---------------------------------------------------------------


def test_the_log_needs_the_demo_password_session(site):
    # Catches: the audit log, which covers every family, being readable without logging in.
    assert site.client.get("/log").status_code == 303


def test_the_log_shows_events_newest_first_and_that_the_chain_is_intact(site):
    # Catches: a log page that hides the proof it is meant to give, or lists oldest first.
    site.login()
    site.client.post("/demo/advance", data={"hours": "2"})
    page = site.client.get("/log").text
    assert "Chain intact" in page
    assert 0 < page.index("demo.advanced") < page.index("seed.demo")


def test_the_log_says_where_the_chain_is_broken(site):
    # Catches: an edited audit row going unnoticed on the page that exists to show tampering.
    site.login()
    with site.connect() as conn:
        conn.execute("DROP TRIGGER audit_no_update")
        conn.execute("UPDATE audit SET actor = 'mallory' WHERE seq = 1")
    page = site.client.get("/log").text
    assert "Chain broken at #1" in page
    assert "Chain intact" not in page


def test_log_text_from_users_is_shown_as_text(site):
    # Catches: text a person or an invoice put into an audit row running as code on the log page.
    site.login()
    with site.connect() as conn:
        note = {"text": "<script>x()</script>"}
        audit.record(conn, at=clock.now(conn), family_id=FAM, actor="m_amina", event="note", data=note)
    page = site.client.get("/log").text
    assert "&lt;script&gt;x()&lt;/script&gt;" in page or "\\u003cscript" in page
    assert "<script>x()" not in page


def test_the_demo_controls_need_the_demo_password_session(site):
    # Catches: anyone being able to move the clock or wipe the database.
    assert site.client.get("/demo").status_code == 303
    assert site.client.post("/demo/advance", data={"hours": "1"}).status_code == 303
    assert site.client.post("/demo/reset").status_code == 303
    assert site.query("SELECT COUNT(*) AS n FROM families")[0]["n"] == 2


def test_the_demo_page_is_labelled_demo_controls(site):
    # Catches: demo buttons that look like part of the real product.
    site.login()
    assert "Demo controls" in site.client.get("/demo").text


def test_advancing_the_demo_clock_shows_a_banner_and_is_logged(site):
    # Catches: a fast-forwarded clock that nobody can see, so expiry and waiting periods look like bugs.
    site.login()
    assert "Demo time" not in site.client.get("/chat").text
    site.client.post("/demo/advance", data={"hours": "24"})
    assert "Demo time: +24h" in site.client.get("/chat").text
    with site.connect() as conn:
        assert clock.offset(conn) == 24 * 3600
        assert [e["data"] for e in audit.events(conn, None) if e["event"] == "demo.advanced"] == [{"hours": 24}]


@pytest.mark.parametrize("hours", ["0", "-5", "abc", "", "721", "1.5", "1_0"])
def test_a_silly_number_of_hours_does_not_move_the_clock(site, hours):
    # Catches: a negative or huge jump, or an unreadable number, breaking every timestamp in the demo.
    site.login()
    response = site.client.post("/demo/advance", data={"hours": hours})
    assert response.status_code == 400
    with site.connect() as conn:
        assert clock.offset(conn) == 0


def test_reset_without_a_demo_pin_keeps_the_database(settings):
    # Catches: a reset that deletes the database and then can't re-seed it, leaving nobody able to log in.
    site = Site(replace(settings, demo_pin=None))
    site.login()
    with pytest.raises(RuntimeError, match="DEMO_PIN"):
        site.client.post("/demo/reset")
    assert site.query("SELECT COUNT(*) AS n FROM families")[0]["n"] == 2


def test_reset_starts_the_demo_again_from_a_fresh_database(site):
    # Catches: a reset that leaves frozen flags, old audit rows or a moved clock behind, or loses the demo PIN.
    site.login("baraka")
    site.chat("stop")
    site.client.post("/demo/advance", data={"hours": "5"})
    response = site.client.post("/demo/reset")
    assert response.status_code == 303
    assert site.query("SELECT frozen FROM families WHERE id = ?", FAM)[0]["frozen"] == 0
    with site.connect() as conn:
        assert clock.offset(conn) == 0
        assert not [e for e in audit.events(conn, None) if e["event"] == "family.frozen"]
        assert approvals.verify_pin(conn, FAM, "m_amina", PIN, PEPPER, clock.now(conn)).ok
    assert site.client.get("/chat").status_code == 200  # the browser stays logged in


NAIROBI = ZoneInfo("Africa/Nairobi")


def set_up_an_allowance(site) -> None:
    """Funds, and an approved standing order of GBP 500 to the student, as if set earlier."""
    with site.connect() as conn:
        funds = Money.parse("5000000", "KES")
        ledger.post(conn, family_id=FAM, at=clock.now(conn), amount=funds, kind="collection", ref="funds")
        conn.execute(
            "INSERT INTO standing_orders (id, family_id, payee_id, amount_minor, currency, day_of_month, approved_by, "
            "active) VALUES ('so_web', ?, 'tp_jabali', 50000, 'GBP', 1, 'm_amina', 1)",
            (FAM,),
        )


def test_the_demo_page_offers_the_jump_to_next_month(site):
    # Catches: a demo with no way to show the monthly allowance without waiting a month.
    site.login()
    assert "Jump to the 1st of next month" in site.client.get("/demo").text


def test_the_jump_needs_the_demo_password_session(site):
    # Catches: anyone being able to move the clock a month and set off the scheduler.
    assert site.client.post("/demo/next-month").status_code == 303
    with site.connect() as conn:
        assert clock.offset(conn) == 0


def test_the_jump_lands_on_nine_on_the_first_of_next_month_and_runs_a_tick(site):
    # Catches: a jump that lands in the wrong month or timezone, or moves the clock without Karo noticing.
    set_up_an_allowance(site)
    site.login()
    with site.connect() as conn:
        before = clock.now(conn).astimezone(NAIROBI)
    response = site.client.post("/demo/next-month")
    assert response.status_code == 303 and response.headers["location"] == "/demo"
    with site.connect() as conn:
        after = clock.now(conn).astimezone(NAIROBI)
        assert (after.year, after.month) == (before.year + before.month // 12, before.month % 12 + 1)
        assert (after.day, after.hour, after.minute) == (1, 9, 0)
        assert [r["kind"] for r in conn.execute("SELECT kind FROM payouts")] == ["allowance_run"]
        (moved,) = [e for e in audit.events(conn, None) if e["event"] == "demo.advanced"]
        assert moved["actor"] == "demo"
    assert "1st of next month" in site.client.get("/demo").text


def test_the_jump_is_not_held_to_the_hours_limit(site):
    # Catches: the 720-hour limit on the hours box stopping the jump in a 31-day month from the 1st.
    site.login()
    with site.connect() as conn:
        clock.advance(conn, 3600 * 24 * 45)
    assert site.client.post("/demo/next-month").status_code == 303


class FakeScheduler:
    """Stands in for APScheduler's BackgroundScheduler, so no test starts a thread."""

    made: list = []

    def __init__(self, *args, **kwargs) -> None:
        self.jobs: list = []
        self.started = self.stopped = False
        FakeScheduler.made.append(self)

    def add_job(self, func, trigger, **kwargs) -> None:
        self.jobs.append((func, trigger, kwargs))

    def start(self) -> None:
        self.started = True

    def shutdown(self, wait: bool = True) -> None:
        self.stopped = True


@pytest.fixture
def fake_scheduler(monkeypatch):
    FakeScheduler.made = []
    monkeypatch.setattr(web_app, "BackgroundScheduler", FakeScheduler)
    return FakeScheduler.made


def test_an_app_made_by_default_starts_no_scheduler(settings, fake_scheduler):
    # Catches: every test app, and every second app in one process, starting a clock-driven thread that pays people.
    app = create_app(settings, client=FakeClaude([]), provider=StandInProvider.from_config(SECRET, BASE_URL))
    with TestClient(app):
        pass
    assert fake_scheduler == []


def test_a_started_server_ticks_every_30_seconds_and_stops_the_scheduler_at_shutdown(settings, fake_scheduler):
    # Catches: a scheduler that never starts, ticks at the wrong pace, or outlives the server.
    provider = StandInProvider.from_config(SECRET, BASE_URL)
    app = create_app(settings, client=FakeClaude([]), provider=provider, start_scheduler=True)
    assert fake_scheduler == []  # building the app starts nothing; the server's startup does
    with TestClient(app):
        (background,) = fake_scheduler
        assert background.started and not background.stopped
        ((func, trigger, options),) = background.jobs
        assert (func, trigger, options["seconds"]) == (run_scheduled_tick, "interval", 30)
        assert options["args"] == [settings, provider]
        assert options["max_instances"] == 1  # a slow tick must not be overlapped by the next
    assert background.stopped


def test_the_uvicorn_entry_asks_for_the_scheduler(settings, monkeypatch):
    # Catches: `uvicorn karo.web.app:app` serving the demo with no allowance runs, reminders or reconciling.
    asked = {}
    monkeypatch.setattr(web_app, "load_settings", lambda: settings)
    monkeypatch.setattr(web_app, "create_app", lambda given, **options: asked.update(options) or FastAPI())
    monkeypatch.setattr(web_app, "_app", None)
    assert isinstance(web_app.app, FastAPI)
    assert asked == {"start_scheduler": True}


def test_a_scheduled_tick_follows_the_demo_clock_on_its_own_connection(site):
    # Catches: a tick that reads real time, so a fast-forwarded demo never sends the reminder it is meant to show.
    ask_for_a_share(site)
    with site.connect() as conn:
        clock.advance(conn, 25 * 3600)
    run_scheduled_tick(site.settings, site.provider)
    reminders = [r["text"] for r in site.query("SELECT text FROM outbox WHERE member_id = 'm_baraka'")]
    assert len([t for t in reminders if t.startswith("Reminder:")]) == 1


# --- money in: the stand-in checkout and the provider's notices -----------------------------------


class NotTheStandIn:
    """A provider that is not the stand-in: its name is enough for the routes to decide against it."""

    name = "flutterwave"


def ask_for_a_share(site, member_id: str = "m_baraka", amount: str = "3000.00") -> dict:
    """A fee with one share, asked for through the provider. Returns the collection row and the share's id."""
    with site.connect() as conn:
        now = clock.now(conn)
        payee = store.load_state(conn, FAM, now).trusted_payees[0].payee
        fee_id = store.insert_fee(
            conn,
            family_id=FAM,
            payee=payee,
            amount=Money.parse("6000.00", "GBP"),
            due_date=None,
            invoice_ref=None,
            now=now,
        )
        share_id = db.new_id("sh")
        conn.execute(
            "INSERT INTO shares (id, family_id, fee_id, member_id, amount_minor, currency, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'GBP', ?)",
            (share_id, FAM, fee_id, member_id, Money.parse(amount, "GBP").minor, clock.iso(now)),
        )
        collection_id = collections.request_share(
            conn, site.provider, family_id=FAM, share_id=share_id, now=now, base_url=BASE_URL
        )
        row = conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
        return {"collection": dict(row), "share_id": share_id}


def checkout_path(asked: dict) -> str:
    return f"/standin/pay/{asked['collection']['tx_ref']}"


def share_paid(site, asked: dict) -> int:
    return site.query("SELECT paid_minor FROM shares WHERE id = ?", asked["share_id"])[0]["paid_minor"]


def signed(body: bytes, secret: str = SECRET) -> dict[str, str]:
    return {SIGNATURE_HEADER: hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}


def test_the_checkout_page_says_it_is_simulated_and_shows_the_amount_with_a_pay_button(site):
    # Catches: a practice checkout that looks real, or that doesn't say how much is being paid.
    asked = ask_for_a_share(site)
    page = site.client.get(checkout_path(asked))
    amount = str(Money(asked["collection"]["amount_minor"], "KES"))
    assert page.status_code == 200
    assert "Stand-in checkout (simulated, no real money)" in page.text
    assert amount in page.text and "<button" in page.text and 'method="post"' in page.text


def test_paying_at_the_checkout_credits_the_share_and_shows_the_thank_you_page(site):
    # Catches: a Pay button that does nothing, or one that credits the share but strands the payer.
    asked = ask_for_a_share(site)
    response = site.client.post(checkout_path(asked))
    assert response.status_code == 303 and response.headers["location"] == "/paid"
    assert share_paid(site, asked) == Money.parse("3000.00", "GBP").minor
    assert site.query("SELECT status FROM collections")[0]["status"] == "succeeded"
    assert "Payment received. You can close this page." in site.client.get("/paid").text


def test_pressing_pay_twice_credits_once(site):
    # Catches: a double click on Pay crediting the same share twice.
    asked = ask_for_a_share(site)
    assert site.client.post(checkout_path(asked)).status_code == 303
    assert site.client.post(checkout_path(asked)).status_code == 303
    assert share_paid(site, asked) == Money.parse("3000.00", "GBP").minor
    assert len(site.query("SELECT seq FROM ledger")) == 1


def test_a_paid_checkout_no_longer_offers_the_pay_button(site):
    # Catches: a finished payment still showing a Pay button, which invites a second try.
    asked = ask_for_a_share(site)
    site.client.post(checkout_path(asked))
    page = site.client.get(checkout_path(asked))
    assert page.status_code == 200 and "<button" not in page.text and "already been made" in page.text


def test_a_checkout_for_a_payment_nobody_asked_for_is_not_found(site):
    # Catches: the checkout answering for any reference, or crashing on one it doesn't know.
    assert site.client.get("/standin/pay/karo-col_nobody").status_code == 404
    assert site.client.post("/standin/pay/karo-col_nobody").status_code == 404


def test_a_checkout_the_stand_in_has_forgotten_is_not_found_rather_than_a_crash(make_site, site):
    # Catches: a restart (the stand-in keeps payments in memory) turning an old link into a 500 error page.
    asked = ask_for_a_share(site)
    restarted = make_site()
    response = restarted.client.post(checkout_path(asked))
    assert response.status_code == 404 and "Ask the main payer to send a reminder for a new link" in response.text
    assert share_paid(restarted, asked) == 0


def test_the_checkout_does_not_exist_unless_the_provider_is_the_stand_in(settings, site):
    # Catches: the pretend "pay" button staying live when a real provider is configured, so anyone can mark shares paid.
    asked = ask_for_a_share(site)
    real = Site(settings, provider=NotTheStandIn())
    assert real.client.get(checkout_path(asked)).status_code == 404
    assert real.client.post(checkout_path(asked)).status_code == 404
    assert share_paid(real, asked) == 0


def test_a_signed_webhook_credits_the_share_with_no_login(site):
    # Catches: the provider's notice needing a login it can never have, so no payment is ever recorded.
    asked = ask_for_a_share(site)
    headers, body = site.provider.mark_paid(asked["collection"]["tx_ref"])
    response = site.client.post("/webhooks/standin", content=body, headers=headers)
    assert response.status_code == 200 and response.json() == {"result": "credited"}
    assert share_paid(site, asked) == Money.parse("3000.00", "GBP").minor


def test_the_webhook_checks_the_signature_against_the_exact_bytes_sent(site):
    # Catches: parsing and re-encoding the body before checking it, which breaks every signature but a test's.
    asked = ask_for_a_share(site)
    tx_ref = asked["collection"]["tx_ref"]
    site.provider.mark_paid(tx_ref)
    body = json.dumps({"tx_ref": tx_ref, "event_id": "SI-EVT-spaced"}, indent=4).encode()
    response = site.client.post("/webhooks/standin", content=body, headers=signed(body))
    assert response.status_code == 200 and response.json() == {"result": "credited"}


def test_a_forged_webhook_is_refused_and_credits_nothing(site):
    # Catches: a made-up notice from anyone on the internet paying a share.
    asked = ask_for_a_share(site)
    body = json.dumps({"event_id": "x", "tx_ref": asked["collection"]["tx_ref"]}).encode()
    response = site.client.post("/webhooks/standin", content=body, headers=signed(body, "not-the-secret"))
    assert response.status_code == 401 and response.json() == {"result": "rejected"}
    assert share_paid(site, asked) == 0


def test_a_signed_webhook_the_provider_does_not_confirm_is_refused(site):
    # Catches: believing a notice for a payment the provider says never happened.
    asked = ask_for_a_share(site)
    body = json.dumps({"event_id": "SI-EVT-early", "tx_ref": asked["collection"]["tx_ref"]}).encode()
    response = site.client.post("/webhooks/standin", content=body, headers=signed(body))
    assert response.status_code == 401 and response.json() == {"result": "unverified"}
    assert share_paid(site, asked) == 0


def test_a_repeated_webhook_and_an_unknown_one_are_both_acknowledged(site):
    # Catches: answering a retry or an unrelated notice with an error, which makes the provider retry for days.
    asked = ask_for_a_share(site)
    headers, body = site.provider.mark_paid(asked["collection"]["tx_ref"])
    site.client.post("/webhooks/standin", content=body, headers=headers)
    again = site.client.post("/webhooks/standin", content=body, headers=headers)
    assert again.status_code == 200 and again.json() == {"result": "duplicate"}
    stranger = json.dumps({"event_id": "SI-EVT-other", "tx_ref": "karo-col_nobody"}).encode()
    unknown = site.client.post("/webhooks/standin", content=stranger, headers=signed(stranger))
    assert unknown.status_code == 200 and unknown.json() == {"result": "unknown"}
    assert share_paid(site, asked) == Money.parse("3000.00", "GBP").minor


def test_a_webhook_for_a_provider_this_app_does_not_use_is_not_found(site):
    # Catches: a notice for one provider being judged by another's rules.
    asked = ask_for_a_share(site)
    headers, body = site.provider.mark_paid(asked["collection"]["tx_ref"])
    response = site.client.post("/webhooks/flutterwave", content=body, headers=headers)
    assert response.status_code == 404
    assert share_paid(site, asked) == 0


def split_token(site) -> str:
    """Has Amina's agent propose a split of a GBP 6,000 fee between Baraka and Nakato. Returns her confirm token."""
    with site.connect() as conn:
        now = clock.now(conn)
        payee = store.load_state(conn, FAM, now).trusted_payees[0].payee
        total = Money.parse("6000.00", "GBP")
        fee_id = store.insert_fee(
            conn, family_id=FAM, payee=payee, amount=total, due_date=None, invoice_ref="INV-77", now=now
        )
        split = (("m_baraka", Money.parse("3500.00", "GBP")), ("m_nakato", Money.parse("2500.00", "GBP")))
        proposals.propose(
            conn,
            family_id=FAM,
            actor=store.member(conn, FAM, "m_amina"),
            kind=Kind.SPLIT,
            now=now,
            base_url=BASE_URL,
            amount=total,
            fee_id=fee_id,
            split=split,
        )
        row = conn.execute(
            "SELECT text FROM outbox WHERE member_id = 'm_amina' AND text LIKE '%/confirm/%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return re.search(r"/confirm/(\S+)", row["text"]).group(1)


def test_confirming_a_split_on_the_pin_page_sends_the_payment_links(site):
    # Catches: the PIN page running the split without the app's own address, so payers come back to the wrong page.
    # The page hides an error from the executor, so a missing link is how a failure would show.
    return_pages = []
    create = site.provider.create_collection
    site.provider.create_collection = lambda **kw: return_pages.append(kw["redirect_url"]) or create(**kw)
    page = enter(site, split_token(site))
    assert page.status_code == 200 and "Asked Baraka, Nakato" in words(page) and PROCESSING not in words(page)
    for member_id in ("m_baraka", "m_nakato"):
        links = site.query("SELECT text FROM outbox WHERE member_id = ? AND text LIKE '%/standin/pay/%'", member_id)
        assert len(links) == 1
    assert return_pages == [f"{BASE_URL}/paid"] * 2


class DropsTheSecondLink(StandInProvider):
    def create_collection(self, **kwargs):
        if self._amounts:
            raise ConnectionError("connection dropped")
        return super().create_collection(**kwargs)


def test_a_split_whose_links_partly_fail_is_done_and_says_what_is_left(make_site):
    # Catches: a split that is saved but reported as "being processed" when some links never went out.
    site = make_site(provider=DropsTheSecondLink.from_config(SECRET, BASE_URL))
    page = enter(site, split_token(site))
    text = words(page)
    assert page.status_code == 200 and "<h1>Done</h1>" in page.text
    assert "Some payment links couldn't be sent yet; Karo will retry when you send reminders." in text
    assert PROCESSING not in text
    assert status_of_only_proposal(site) == "executed"
    assert len(site.query("SELECT id FROM shares")) == 2 and len(site.query("SELECT id FROM collections")) == 1
    told = site.query("SELECT text FROM outbox WHERE member_id = 'm_amina' AND text LIKE 'Some payment links%'")
    assert len(told) == 1


def test_a_link_that_was_replaced_says_so_and_cannot_be_paid(site):
    # Catches: a replaced link still showing a Pay button (or "already paid"), so a payer pays a share twice.
    asked = ask_for_a_share(site)
    with site.connect() as conn:
        conn.execute("UPDATE collections SET status = 'failed'")
    page = site.client.get(checkout_path(asked))
    assert page.status_code == 200 and "<button" not in page.text and "no longer valid" in page.text
    assert site.client.post(checkout_path(asked)).status_code == 404
    assert share_paid(site, asked) == 0
