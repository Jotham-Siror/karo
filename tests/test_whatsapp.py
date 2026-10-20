"""The WhatsApp door: the signed Twilio webhook, the files it downloads, and the messages it sends back.
Every test names the bug it would catch. Nothing here reaches the network: Twilio's sender is a fake, and the
file downloads are mocked with respx."""

import base64
import json
import logging
import re
import shutil
import threading
import time
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from twilio.base.exceptions import TwilioRestException
from twilio.http import HttpClient
from twilio.http.response import Response as TwilioResponse
from twilio.request_validator import RequestValidator

from karo import audit, clock, db, outbox, seed, store
from karo.agent.claude import FakeClaude, text_response, tool_response
from karo.config import Settings
from karo.doors import base, whatsapp
from karo.domain import Payee
from karo.money import Money
from karo.providers.standin import StandInProvider
from karo.web.app import create_app, run_scheduled_tick

FAM = "fam_otieno"
PIN = "4821"
PEPPER = "test-pepper"
PASSWORD = "demo-password-xyz"
SECRET = "standin-secret-xyz"
PUBLIC_URL = "https://karo.example.test"
TWILIO_SID = "ACtestsid0000000000000000000000"
TWILIO_TOKEN = "twilio-token-xyz"
TWILIO_FROM = "whatsapp:+14155550100"

AMINA = "whatsapp:+254700000101"
BARAKA = "whatsapp:+254700000102"
NAKATO = "whatsapp:+256770000103"
PHONES = {"amina": "+254700000101", "baraka": "+254700000102", "nakato": "+256770000103"}

WEBHOOK = "/webhooks/twilio"
SIGNATURE = "X-Twilio-Signature"
MEDIA_URL = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages/MM01/Media/ME01"
CDN_URL = "https://media.twiliocdn.test/ME01/file"
PDF_BYTES = b"%PDF-1.4 invoice"
FIVE_MB = 5 * 1024 * 1024


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    # Seeding hashes two PINs with Argon2, which is deliberately slow: do it once and copy the file per test.
    path = tmp_path_factory.mktemp("template") / "karo.db"
    connection = db.connect(path)
    db.init(connection)
    seed.seed_demo(connection, pin=PIN, pepper=PEPPER, now=datetime.now(UTC), whatsapp=PHONES)
    connection.close()
    return path


def make_settings(path, **changes) -> Settings:
    base = Settings(
        db_path=str(path),
        public_base_url=PUBLIC_URL,
        pin_pepper=PEPPER,
        session_secret="session-secret-xyz",
        demo_password=PASSWORD,
        standin_secret=SECRET,
        demo_pin=PIN,
        twilio_account_sid=TWILIO_SID,
        twilio_auth_token=TWILIO_TOKEN,
        twilio_whatsapp_from=TWILIO_FROM,
        demo_whatsapp=dict(PHONES),
    )
    return replace(base, **changes)


@pytest.fixture
def settings(seeded_db, tmp_path):
    target = tmp_path / "karo.db"
    shutil.copyfile(seeded_db, target)
    return make_settings(target)


def whatsapp_identities(settings: Settings) -> dict[str, str]:
    with closing(db.connect(settings.db_path)) as conn:
        rows = conn.execute("SELECT address, member_id FROM identities WHERE channel = 'whatsapp'").fetchall()
    return {row["address"]: row["member_id"] for row in rows}


# --- the demo phones are seeded, and a reset keeps them ------------------------------------------


def test_a_new_database_is_seeded_with_the_demo_phones(tmp_path):
    # Catches: the phones in .env being ignored on first start, so every real message is "not linked to a family".
    fresh = make_settings(tmp_path / "fresh.db")

    create_app(fresh, client=FakeClaude([]), provider=StandInProvider.from_config(SECRET, PUBLIC_URL))

    assert whatsapp_identities(fresh) == {AMINA: "m_amina", BARAKA: "m_baraka", NAKATO: "m_nakato"}


def test_a_demo_reset_keeps_the_phones(settings):
    # Catches: the reset building a database with no phones, so the demo's WhatsApp stops answering after a reset.
    app = create_app(settings, client=FakeClaude([]), provider=StandInProvider.from_config(SECRET, PUBLIC_URL))
    # https, like the real address: the session cookie is marked Secure and would not be sent over plain http.
    browser = TestClient(app, base_url=PUBLIC_URL, follow_redirects=False)
    assert browser.post("/login", data={"password": PASSWORD, "person": "amina"}).status_code == 303
    with closing(db.connect(settings.db_path)) as conn:
        audit.record(conn, at=datetime.now(UTC), family_id=FAM, actor="test", event="marker", data={})

    assert browser.post("/demo/reset").status_code == 303

    assert whatsapp_identities(settings) == {AMINA: "m_amina", BARAKA: "m_baraka", NAKATO: "m_nakato"}
    with closing(db.connect(settings.db_path)) as conn:
        assert [e["event"] for e in audit.events(conn, FAM, limit=1000) if e["event"] == "marker"] == []


# --- long replies are cut into pieces Twilio accepts ----------------------------------------------


def words_text(count: int) -> str:
    return " ".join(f"word{i}" for i in range(count))


def test_a_short_reply_is_one_message():
    # Catches: a short reply being padded, split or lost on its way through the splitter.
    assert whatsapp.chunks("Hello Amina.") == ["Hello Amina."]


def test_a_long_reply_is_cut_between_words_into_pieces_of_at_most_1500_characters():
    # Catches: a reply over Twilio's length limit being refused whole, or a word cut in half or dropped at a seam.
    text = words_text(450)
    assert len(text) > 3000

    pieces = whatsapp.chunks(text)

    assert len(pieces) == 3
    assert all(0 < len(piece) <= whatsapp.CHUNK_SIZE for piece in pieces)
    assert " ".join(pieces).split() == text.split()


def test_a_word_longer_than_a_message_is_cut_at_the_limit_not_lost():
    # Catches: the splitter looping forever or dropping text when there is no space to cut at.
    pieces = whatsapp.chunks("x" * 1501)
    assert [len(piece) for piece in pieces] == [1500, 1]


def test_a_reply_of_exactly_1500_characters_is_not_split():
    # Catches: an off-by-one that splits a reply that fits, sending a lone word as a second message.
    assert whatsapp.chunks("y" * 1500) == ["y" * 1500]


def test_an_empty_reply_makes_no_message():
    # Catches: an empty message being sent to Twilio, which refuses it and fills the log with errors.
    assert whatsapp.chunks("  \n ") == []


# --- the real sender, with Twilio's own client and a fake HTTP layer -------------------------------


class RecordingHttp(HttpClient):
    """Twilio's own client code runs; only the HTTP call at the bottom is replaced."""

    def __init__(self) -> None:
        super().__init__(logging.getLogger("test.twilio"), is_async=False)
        self.requests: list[dict] = []

    def request(
        self, method, uri, params=None, data=None, headers=None, auth=None, timeout=None, allow_redirects=False
    ):
        self.requests.append({"method": method, "uri": uri, "data": data, "auth": auth})
        return TwilioResponse(201, json.dumps({"sid": "SM0000", "status": "queued"}))


def test_the_twilio_sender_posts_the_text_from_karos_number_to_the_person():
    # Catches: from and to swapped, or the text sent under the wrong field, so nothing reaches the person's phone.
    http = RecordingHttp()
    send = whatsapp.twilio_sender(TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, http_client=http)

    send(AMINA, "Hello Amina.")

    (request,) = http.requests
    assert request["method"] == "POST" and request["uri"].endswith(f"/Accounts/{TWILIO_SID}/Messages.json")
    assert request["data"] == {"From": TWILIO_FROM, "To": AMINA, "Body": "Hello Amina."}
    assert request["auth"] == (TWILIO_SID, TWILIO_TOKEN)


# --- the door, with a scripted AI and a fake Twilio ------------------------------------------------


class FakeSender:
    """What Twilio would be asked to send, in order. Set `fail` to make Twilio refuse."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail: Exception | None = None

    def __call__(self, to: str, body: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent.append((to, body))

    def to(self, address: str) -> list[str]:
        return [body for to, body in self.sent if to == address]


def sign(form: dict, token: str = TWILIO_TOKEN, url: str = PUBLIC_URL + WEBHOOK) -> str:
    return RequestValidator(token).compute_signature(url, form)


def inbound(body: str = "hello", *, sid: str = "SM0001", sender: str = AMINA, **extra: str) -> dict[str, str]:
    """The form Twilio posts for a WhatsApp message."""
    form = {
        "MessageSid": sid,
        "AccountSid": TWILIO_SID,
        "From": sender,
        "To": TWILIO_FROM,
        "Body": body,
        "NumMedia": "0",
        "SmsStatus": "received",
    }
    return form | extra


class Door:
    """The app with the WhatsApp door on. Posts run their background work before they return, as in TestClient."""

    def __init__(self, settings: Settings, steps=(), *, provider=None, sender: FakeSender | None = None) -> None:
        self.settings = settings
        self.claude = FakeClaude(steps)
        self.sender = sender or FakeSender()
        self.provider = provider or StandInProvider.from_config(SECRET, PUBLIC_URL)
        app = create_app(settings, client=self.claude, provider=self.provider, whatsapp_sender=self.sender)
        # http://testserver, not the public address: the signature must hold whatever host the request arrives on.
        self.client = TestClient(app, follow_redirects=False)

    def receive(self, form: dict[str, str]):
        return self.client.post(WEBHOOK, data=form, headers={SIGNATURE: sign(form)})

    def query(self, sql: str, *args):
        with closing(db.connect(self.settings.db_path)) as conn:
            return conn.execute(sql, args).fetchall()

    def count(self, table: str, where: str = "1 = 1") -> int:
        return self.query(f"SELECT COUNT(*) FROM {table} WHERE {where}")[0][0]

    def events(self, name: str) -> list[dict]:
        with closing(db.connect(self.settings.db_path)) as conn:
            return [e for e in audit.events(conn, None, limit=1000) if e["event"] == name]


# --- a message not really from Twilio ------------------------------------------------------------

FORGERIES = [
    "signed_with_another_token",
    "no_signature_at_all",
    "body_changed_after_signing",
    "signed_for_the_hosts_address",
]


@pytest.mark.parametrize("forgery", FORGERIES)
def test_a_message_not_really_from_twilio_is_refused_and_leaves_no_trace_of_its_content(settings, forgery):
    # Catches: anyone who knows the webhook address talking to the agent as Amina, or a forged message burning an id.
    door = Door(settings, [text_response("This must never be said.")])
    form = inbound("Pay the university fees now, no PIN needed.")
    signatures = {
        "signed_with_another_token": {SIGNATURE: sign(form, token="another-token")},
        "no_signature_at_all": {},
        "body_changed_after_signing": {SIGNATURE: sign(form | {"Body": "Hello"})},
        # What a server that trusted its Host header would check: the address the request seemed to arrive on.
        "signed_for_the_hosts_address": {SIGNATURE: sign(form, url="http://testserver" + WEBHOOK)},
    }

    response = door.client.post(WEBHOOK, data=form, headers=signatures[forgery])

    assert response.status_code == 403
    assert door.claude.requests == [] and door.sender.sent == []
    assert door.count("inbound_messages") == 0 and door.count("proposals") == 0
    (rejected,) = door.events("inbound.rejected")
    assert rejected["data"] == {"channel": "whatsapp"}
    everything_logged = json.dumps(door.events("inbound.rejected"))
    for content in ("Pay the university fees", AMINA, "+254700000101", "SM0001"):
        assert content not in everything_logged


def test_a_form_with_a_file_in_it_is_refused_not_crashed_on(settings):
    # Catches: a multipart post with a file field reaching the signature check and ending in a server error.
    door = Door(settings, [])

    response = door.client.post(WEBHOOK, files={"Body": ("a.txt", b"hello")}, headers={SIGNATURE: "x"})

    assert response.status_code == 403
    assert door.claude.requests == []


def test_a_signed_message_with_no_sender_or_id_is_a_bad_request_and_goes_no_further(settings):
    # Catches: a signed but incomplete post failing silently in the background task, so nobody ever learns why.
    door = Door(settings, [text_response("This must never be said.")])

    no_id = {k: v for k, v in inbound().items() if k != "MessageSid"}
    no_sender = {k: v for k, v in inbound(sid="SM0002").items() if k != "From"}

    assert [door.receive(form).status_code for form in (no_id, no_sender)] == [400, 400]
    assert door.claude.requests == [] and door.count("inbound_messages") == 0


def test_the_door_is_not_there_without_all_three_twilio_settings(settings):
    # Catches: a webhook open on a server with no Twilio token to check it against.
    door = Door(replace(settings, twilio_auth_token=None), [text_response("This must never be said.")])

    response = door.receive(inbound())

    assert response.status_code == 404
    assert door.claude.requests == [] and door.sender.sent == [] and door.count("inbound_messages") == 0


# --- a message really from Twilio ----------------------------------------------------------------


def test_a_signed_message_reaches_the_agent_and_the_reply_goes_back_to_the_sender(settings):
    # Catches: the door dropping a genuine message, or answering Twilio with a body Twilio would try to read out.
    door = Door(settings, [text_response("Hello Amina.")])

    response = door.receive(inbound("hi Karo"))

    assert response.status_code == 200 and response.text == "<Response/>"
    assert response.headers["content-type"].startswith("text/xml")
    assert door.claude.requests[0]["messages"][-1]["content"] == [{"type": "text", "text": "hi Karo"}]
    assert door.sender.sent == [(AMINA, "Hello Amina.")]
    assert door.count("inbound_messages", "channel = 'whatsapp' AND message_id = 'SM0001'") == 1


def test_the_same_message_sid_twice_is_handled_once(settings):
    # Catches: Twilio's retry running the agent twice, so Amina gets two PIN requests and two replies.
    door = Door(settings, [text_response("First."), text_response("Second.")])

    first = door.receive(inbound(sid="SM-same"))
    second = door.receive(inbound(sid="SM-same"))

    assert (first.status_code, second.status_code) == (200, 200)
    assert len(door.claude.requests) == 1
    assert door.sender.sent == [(AMINA, "First.")]


def test_an_unknown_number_gets_the_polite_refusal_and_no_agent(settings):
    # Catches: a stranger's message reaching the agent, or being ignored with no word back.
    stranger = "whatsapp:+254799999999"
    door = Door(settings, [text_response("This must never be said.")])

    door.receive(inbound("hello?", sender=stranger))

    assert door.sender.sent == [(stranger, base.UNKNOWN_SENDER)]
    assert door.claude.requests == []


def test_a_long_reply_arrives_as_several_messages_in_order(settings):
    # Catches: a long reply refused by Twilio as one message, so the person gets nothing at all.
    long_reply = " ".join(f"word{i}" for i in range(450))
    door = Door(settings, [text_response(long_reply)])

    door.receive(inbound())

    bodies = door.sender.to(AMINA)
    assert len(bodies) == 3 and all(len(body) <= whatsapp.CHUNK_SIZE for body in bodies)
    assert " ".join(bodies).split() == long_reply.split()


# --- files sent with the message -----------------------------------------------------------------


def with_media(content_type: str, **extra: str) -> dict[str, str]:
    return inbound("Here is the invoice", NumMedia="1", MediaUrl0=MEDIA_URL, MediaContentType0=content_type, **extra)


def test_a_pdf_is_downloaded_with_the_account_login_and_given_to_the_agent(settings):
    # Catches: the invoice never reaching the agent, or Twilio's login being sent on to the CDN it redirects to.
    door = Door(settings, [text_response("Got the invoice.")])

    with respx.mock(assert_all_mocked=True) as network:
        first = network.get(MEDIA_URL).respond(307, headers={"Location": CDN_URL})
        cdn = network.get(CDN_URL).respond(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})
        door.receive(with_media("application/pdf"))

    login = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    assert first.calls.last.request.headers["authorization"] == f"Basic {login}"
    assert "authorization" not in cdn.calls.last.request.headers
    document, text = door.claude.requests[0]["messages"][-1]["content"]
    assert document["type"] == "document" and document["source"]["data"] == base64.b64encode(PDF_BYTES).decode()
    assert text == {"type": "text", "text": "Here is the invoice"}
    assert door.sender.sent == [(AMINA, "Got the invoice.")]


@pytest.mark.parametrize("how", ["audio_type", "declared_too_big", "streamed_too_big", "download_fails"])
def test_a_file_that_cannot_be_used_is_skipped_and_the_reply_says_so(settings, how):
    # Catches: a voice note or a huge file being fetched into memory and handed on, or one bad file losing the message.
    door = Door(settings, [text_response("Noted.")])
    content_type = "audio/ogg" if how == "audio_type" else "application/pdf"

    with respx.mock(assert_all_mocked=True) as network:
        if how == "declared_too_big":
            network.get(MEDIA_URL).respond(200, content=b"x" * (FIVE_MB + 1))
        elif how == "streamed_too_big":
            # No Content-Length, so only counting the bytes as they arrive can stop it.
            network.get(MEDIA_URL).respond(200, stream=httpx.ByteStream(b"x" * (FIVE_MB + 1)))
        elif how == "download_fails":
            network.get(MEDIA_URL).respond(500)
        door.receive(with_media(content_type))

    assert door.claude.requests[0]["messages"][-1]["content"] == [{"type": "text", "text": "Here is the invoice"}]
    (reply,) = door.sender.to(AMINA)
    assert reply.startswith("Noted.") and "couldn't use 1 file" in reply


def test_a_file_with_no_words_and_nothing_readable_is_answered_not_dropped(settings):
    # Catches: a voice note on its own getting no answer at all, because there was nothing to give the agent.
    door = Door(settings, [])

    door.receive(with_media("audio/ogg") | {"Body": ""})

    assert door.claude.requests == []
    (reply,) = door.sender.to(AMINA)
    assert reply.startswith(base.NOTHING_TO_READ) and "couldn't use 1 file" in reply


# --- messages that wait in the outbox ------------------------------------------------------------

WESTBRIDGE = {"country": "GB", "currency": "GBP", "sort_code": "20-40-60", "account_number": "41234567"}
SHARES = [
    {"member_name": "Baraka", "amount": "3000.00"},
    {"member_name": "Nakato", "amount": "2000.00"},
    {"member_name": "Amina", "amount": "1000.00"},
]
CONFIRM_LINK = re.compile(re.escape(PUBLIC_URL) + r"/confirm/(\S+)")


def split_the_fee(door_settings: Settings) -> list:
    """The agent's script for a split of a fee that is already recorded, as if Amina had just asked for it."""
    with closing(db.connect(door_settings.db_path)) as conn:
        fee_id = store.insert_fee(
            conn,
            family_id=FAM,
            payee=Payee.of("University of Westbridge", **WESTBRIDGE),
            amount=Money.parse("6000.00", "GBP"),
            due_date="2026-11-30",
            invoice_ref="INV-77",
            now=datetime.now(UTC),
        )
    split = tool_response(("propose_split", {"fee_id": fee_id, "shares": SHARES}))
    return [split, text_response("Asked Amina to confirm.")]


def test_the_confirm_link_sent_on_whatsapp_is_on_the_public_address_and_goes_only_to_the_main_payer(settings):
    # Catches: a PIN link that points at localhost (dead on a phone), or one that reaches anyone but Amina.
    door = Door(settings, split_the_fee(settings))

    door.receive(inbound("Please split the fee"))

    reply, link_message = door.sender.to(AMINA)  # the answer first, then the waiting message
    assert reply == "Asked Amina to confirm."
    assert CONFIRM_LINK.search(link_message)
    assert door.sender.to(BARAKA) == [] and door.sender.to(NAKATO) == []
    sent_to_amina = door.query("SELECT channel, delivered_at FROM outbox WHERE member_id = 'm_amina' ORDER BY channel")
    # Only the WhatsApp copy is marked: the web page still has its own copy waiting.
    assert [(row["channel"], row["delivered_at"] is not None) for row in sent_to_amina] == [
        ("web", False),
        ("whatsapp", True),
    ]


def test_a_web_confirm_sends_the_waiting_whatsapp_messages_and_marks_them_delivered(settings):
    # Catches: Baraka and Nakato never getting their payment links, because only the web page was told.
    door = Door(settings, split_the_fee(settings))
    door.receive(inbound("Please split the fee"))
    (token,) = CONFIRM_LINK.findall(door.sender.to(AMINA)[1])

    confirmed = door.client.post(f"/confirm/{token}", data={"pin": PIN})

    assert confirmed.status_code == 200
    asked, pay_link = door.sender.to(BARAKA)  # in the order they were written
    assert "asked you to pay GBP 3,000.00" in asked
    assert pay_link.startswith("Hi Baraka") and f"{PUBLIC_URL}/standin/pay/" in pay_link
    assert f"{PUBLIC_URL}/standin/pay/" in door.sender.to(NAKATO)[-1]
    assert door.count("outbox", "channel = 'whatsapp' AND delivered_at IS NULL") == 0
    assert door.count("outbox", "channel = 'web' AND member_id = 'm_baraka' AND delivered_at IS NULL") == 2


def test_a_message_twilio_refuses_stays_in_the_outbox_and_goes_out_next_time(settings):
    # Catches: a Twilio error losing a message for good (marked delivered), or the error log leaking a secret.
    door = Door(settings, [text_response("One."), text_response("Two.")])
    with closing(db.connect(settings.db_path)) as conn:
        outbox.notify(conn, "m_amina", "Your fee is paid.", datetime.now(UTC))
    secret_uri = f"/Accounts/{TWILIO_SID}/Messages.json"
    door.sender.fail = TwilioRestException(400, secret_uri, "The message body is private text", 63016)

    refused = door.receive(inbound(sid="SM-a"))

    assert refused.status_code == 200
    assert door.count("outbox", "channel = 'whatsapp' AND delivered_at IS NULL") == 1
    failures = door.events("whatsapp.send_failed")
    assert [f["data"] for f in failures] == [{"code": 63016, "error": "TwilioRestException"}] * 2  # the reply, the row
    logged = json.dumps(failures)
    for private in (TWILIO_SID, TWILIO_TOKEN, "private text", "Your fee is paid", AMINA):
        assert private not in logged

    door.sender.fail = None
    door.receive(inbound(sid="SM-b"))

    assert door.sender.to(AMINA) == ["Two.", "Your fee is paid."]
    assert door.count("outbox", "channel = 'whatsapp' AND delivered_at IS NULL") == 0


def test_two_deliveries_at_once_send_each_waiting_message_once(settings):
    # Catches: two people's messages arriving together and both sending the same waiting rows, so Amina gets doubles.
    with closing(db.connect(settings.db_path)) as conn:
        for text in ("First.", "Second.", "Third."):
            outbox.notify(conn, "m_amina", text, datetime.now(UTC))
    sent: list[str] = []

    def slow_sender(to: str, body: str) -> None:
        time.sleep(0.05)  # long enough for the other task to read the same rows if nothing made it wait
        sent.append(body)

    tasks = [threading.Thread(target=whatsapp.deliver_for_family, args=(settings, slow_sender, FAM)) for _ in range(2)]
    for task in tasks:
        task.start()
    for task in tasks:
        task.join()

    assert sorted(sent) == ["First.", "Second.", "Third."]


# --- messages written outside a chat: a payment, a notice, the scheduler, the demo clock -----------


def split_and_confirm(door: Door) -> None:
    """Amina asks on WhatsApp for the fee to be split, then enters her PIN on the web page."""
    door.receive(inbound("Please split the fee"))
    (token,) = CONFIRM_LINK.findall(door.sender.to(AMINA)[1])
    assert door.client.post(f"/confirm/{token}", data={"pin": PIN}).status_code == 200


def baraka_tx_ref(door: Door) -> str:
    return door.query(
        "SELECT c.tx_ref FROM collections c JOIN shares s ON s.id = c.share_id WHERE s.member_id = 'm_baraka'"
    )[0]["tx_ref"]


def test_a_relative_paying_at_the_checkout_tells_amina_on_her_phone(settings):
    # Catches: "Baraka paid" waiting in the outbox until Amina next types something.
    door = Door(settings, split_the_fee(settings))
    split_and_confirm(door)
    heard = len(door.sender.to(AMINA))

    assert door.client.post(f"/standin/pay/{baraka_tx_ref(door)}").status_code == 303

    (told,) = door.sender.to(AMINA)[heard:]
    assert "Baraka" in told


def test_a_payment_notice_from_the_provider_tells_amina_on_her_phone(settings):
    # Catches: a real provider's notice crediting a share without Amina's phone ever hearing of it.
    door = Door(settings, split_the_fee(settings))
    split_and_confirm(door)
    heard = len(door.sender.to(AMINA))
    headers, body = door.provider.mark_paid(baraka_tx_ref(door))

    assert door.client.post("/webhooks/standin", content=body, headers=dict(headers)).status_code == 200

    (told,) = door.sender.to(AMINA)[heard:]
    assert "Baraka" in told


def test_the_scheduled_run_sends_the_messages_it_writes(settings):
    # Catches: the monthly allowance receipt and the reminders waiting in the outbox until someone next writes.
    door = Door(settings, split_the_fee(settings))
    split_and_confirm(door)
    with closing(db.connect(settings.db_path)) as conn:
        clock.advance(conn, 25 * 3600)

    run_scheduled_tick(settings, door.provider, sender=door.sender)

    assert [body for body in door.sender.to(BARAKA) if body.startswith("Reminder:")] != []


def test_the_demo_clock_jump_sends_the_messages_it_writes(settings):
    # Catches: the "jump to next month" button running Karo's checks but leaving their messages off the phones.
    door = Door(settings, split_the_fee(settings))
    split_and_confirm(door)
    browser = TestClient(door.client.app, base_url=PUBLIC_URL, follow_redirects=False)
    assert browser.post("/login", data={"password": PASSWORD, "person": "amina"}).status_code == 303

    assert browser.post("/demo/next-month").status_code == 303

    assert [body for body in door.sender.to(BARAKA) if body.startswith("Reminder:")] != []
