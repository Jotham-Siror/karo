"""The shared front door every channel goes through. Every test names the bug it would catch."""

import base64
import json
import shutil
from datetime import UTC, datetime

import anthropic
import httpx
import pytest

from karo import audit, db, seed
from karo.agent.claude import FakeClaude, text_response
from karo.doors import base
from karo.doors.base import InboundMessage, handle
from karo.providers.standin import StandInProvider

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam_otieno"
PIN = "4821"
BASE_URL = "https://karo.example"


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    # Seeding hashes two PINs with Argon2, which is deliberately slow: do it once and copy the file per test.
    path = tmp_path_factory.mktemp("template") / "karo.db"
    connection = db.connect(path)
    db.init(connection)
    seed.seed_demo(connection, pin=PIN, pepper="test-pepper", now=NOW)
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
    return StandInProvider.from_config("standin-test-secret", BASE_URL)


def message(text="hello", *, message_id="m1", address="amina", channel="web", attachments=()):
    return InboundMessage(channel, message_id, address, text, list(attachments))


def send(conn, provider, claude, msg, *, now=NOW):
    return handle(conn, msg, client=claude, provider=provider, base_url=BASE_URL, now=now)


def events(conn, name):
    return [e for e in audit.events(conn, FAM) if e["event"] == name]


def frozen(conn) -> bool:
    return bool(conn.execute("SELECT frozen FROM families WHERE id = ?", (FAM,)).fetchone()[0])


def last_user_content(claude):
    return claude.requests[-1]["messages"][-1]["content"]


def test_a_reply_from_the_agent_is_returned(conn, provider):
    # Catches: the door swallowing the agent's answer, so the person never sees a reply.
    claude = FakeClaude([text_response("Hello Amina.")])
    assert send(conn, provider, claude, message("hi")) == "Hello Amina."
    assert last_user_content(claude) == [{"type": "text", "text": "hi"}]


def test_the_same_message_id_runs_the_agent_once(conn, provider):
    # Catches: a webhook retry or a double click running the agent twice on one message.
    claude = FakeClaude([text_response("First."), text_response("Second.")])
    first = send(conn, provider, claude, message("hi", message_id="dup-1"))
    second = send(conn, provider, claude, message("hi", message_id="dup-1"))
    assert (first, second) == ("First.", None)
    assert len(claude.requests) == 1


def test_the_same_id_on_another_channel_is_a_different_message(conn, provider):
    # Catches: dedupe keyed on the id alone, so a WhatsApp message could swallow a web message with the same id.
    claude = FakeClaude([text_response("Web."), text_response("WhatsApp.")])
    send(conn, provider, claude, message("hi", message_id="same", channel="web"))
    other = message("hi", message_id="same", channel="whatsapp", address="whatsapp:+254700000101")
    conn.execute(
        "INSERT INTO identities (channel, address, member_id) VALUES ('whatsapp', 'whatsapp:+254700000101', 'm_amina')"
    )
    assert send(conn, provider, claude, other) == "WhatsApp."


def test_an_unknown_sender_gets_a_plain_refusal_and_no_agent_call(conn, provider):
    # Catches: a stranger's message reaching the agent, which would spend money and could read a family's data.
    claude = FakeClaude([])
    reply = send(conn, provider, claude, message("pay the fees", address="stranger"))
    assert reply == "Sorry, this number isn't linked to a Karo family."
    assert claude.requests == []


@pytest.mark.parametrize("word", ["stop", "FREEZE", "  Simamisha \n"])
def test_a_freeze_word_freezes_without_the_agent(conn, provider, word):
    # Catches: the emergency stop going through the AI, which is slow and could be talked out of it.
    claude = FakeClaude([])
    reply = send(conn, provider, claude, message(word, address="baraka"))
    assert frozen(conn)
    assert "frozen" in reply.lower()
    assert claude.requests == []


@pytest.mark.parametrize("text", ["please don't stop", "freeze?", "stop the payment"])
def test_a_freeze_word_inside_a_sentence_does_not_freeze(conn, provider, text):
    # Catches: matching the word anywhere in a message, so ordinary chat freezes the family.
    claude = FakeClaude([text_response("Okay.")])
    send(conn, provider, claude, message(text))
    assert not frozen(conn)
    assert len(claude.requests) == 1


@pytest.mark.parametrize("typed", ["4821", " 123456 ", "0000"])
def test_a_pin_typed_in_chat_is_refused_and_never_kept(conn, provider, typed):
    # Catches: a PIN typed in chat reaching the AI, the conversation history or the log.
    claude = FakeClaude([])
    seeded = {e["seq"] for e in audit.events(conn, None, limit=1000)}
    reply = send(conn, provider, claude, message(typed))
    assert reply == "Never type your PIN in chat. Karo will never ask for it. Use only the PIN page."
    assert claude.requests == []
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    logged = events(conn, "pin.typed_in_chat")
    assert len(logged) == 1
    # Only the data of what this message logged is searched: a hash can contain any four digits by chance, and the
    # seed's opening balance (200000.00) already holds a run of zeros.
    after = [e["data"] for e in audit.events(conn, None, limit=1000) if e["seq"] not in seeded]
    assert after and typed.strip() not in json.dumps(after)


@pytest.mark.parametrize("text", ["123", "1234567", "pay 4821", "48 21", "٤٨٢١"])
def test_other_messages_are_not_taken_for_a_pin(conn, provider, text):
    # Catches: the PIN guard being so wide that amounts, dates and ordinary sentences are refused.
    claude = FakeClaude([text_response("Okay.")])
    assert send(conn, provider, claude, message(text)) == "Okay."
    assert events(conn, "pin.typed_in_chat") == []


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")),
        ConnectionError("provider dropped"),
    ],
)
def test_an_error_in_the_agent_turn_becomes_a_sorry_not_a_crash(conn, provider, error):
    # Catches: a network or provider error escaping as a 500 page in the middle of the demo.
    def fail(_request):
        raise error

    reply = send(conn, provider, FakeClaude([fail]), message("hi"))
    assert reply == "Sorry, something went wrong on my side. Please try again in a minute."
    logged = events(conn, "agent.error")
    assert [e["data"] for e in logged] == [{"error": type(error).__name__}]


def test_a_photo_and_a_pdf_reach_the_agent_as_base64_blocks_before_the_text(conn, provider):
    # Catches: invoice photos and PDFs being dropped, so the agent is asked to read an invoice it cannot see.
    claude = FakeClaude([text_response("Got it.")])
    attachments = [("image/jpeg", b"\xff\xd8photo"), ("application/pdf", b"%PDF-invoice")]
    send(conn, provider, claude, message("here is the invoice", attachments=attachments))

    def source(media_type, data):
        return {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()}

    assert last_user_content(claude) == [
        {"type": "image", "source": source("image/jpeg", b"\xff\xd8photo")},
        {"type": "document", "source": source("application/pdf", b"%PDF-invoice")},
        {"type": "text", "text": "here is the invoice"},
    ]


def test_a_file_the_agent_cannot_read_is_refused_in_plain_words(conn, provider):
    # Catches: an unsupported file going to the API, which answers 400 and shows the person a vague failure.
    claude = FakeClaude([])
    reply = send(conn, provider, claude, message("see this", attachments=[("application/zip", b"PK")]))
    assert "photo" in reply and "PDF" in reply
    assert claude.requests == []


def test_a_file_that_is_too_big_is_refused_in_plain_words(conn, provider):
    # Catches: a huge upload being sent to the paid API, or held in the conversation history.
    claude = FakeClaude([])
    big = ("image/png", b"0" * (base.MAX_ATTACHMENT_BYTES + 1))
    reply = send(conn, provider, claude, message("see this", attachments=[big]))
    assert "too big" in reply
    assert claude.requests == []


def test_too_many_files_are_refused_rather_than_silently_trimmed(conn, provider):
    # Catches: extra files being dropped without a word, so the agent answers about only some of the invoice pages.
    claude = FakeClaude([])
    files = [("image/png", b"x")] * (base.MAX_ATTACHMENTS + 1)
    reply = send(conn, provider, claude, message("see these", attachments=files))
    assert "up to 5 files" in reply
    assert claude.requests == []


def test_an_empty_message_is_not_sent_to_the_agent(conn, provider):
    # Catches: an empty message reaching the API, which rejects it with a 400.
    claude = FakeClaude([])
    reply = send(conn, provider, claude, message("   "))
    assert reply == "Please type a message or send a photo or PDF."
    assert claude.requests == []
