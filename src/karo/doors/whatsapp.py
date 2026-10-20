"""The WhatsApp door, through Twilio: what comes in, what goes out, and the outbox rows that wait for a phone.

The web route (karo.web.app) checks the signature and answers Twilio at once. Everything slower than that, which is
the file downloads, the agent and the sending, runs here in a background task with its own database connection.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing

import httpx
from twilio.base.exceptions import TwilioException
from twilio.http import HttpClient
from twilio.http.http_client import TwilioHttpClient
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from karo import audit, clock, db, outbox, store
from karo.config import Settings
from karo.doors import base
from karo.doors.base import InboundMessage
from karo.providers.base import Provider

logger = logging.getLogger(__name__)

CHANNEL = "whatsapp"
WEBHOOK_PATH = "/webhooks/twilio"
EMPTY_TWIML = "<Response/>"

# Twilio refuses a WhatsApp body over 1,600 characters. Kept under it, so a count that differs by a few can't tip
# it over.
CHUNK_SIZE = 1500
SEND_TIMEOUT_SECONDS = 15
MEDIA_TIMEOUT = httpx.Timeout(20.0, connect=5.0)

# send(to, body): one message to one WhatsApp address (`whatsapp:+254...`). A fake in tests, Twilio in production.
Sender = Callable[[str, str], None]

# Delivery reads the waiting rows and then marks them. Two tasks at once would both read the same rows and send twice,
# so they take turns. One process only, which is how Karo runs.
_DELIVERY = threading.Lock()


# --- the signed webhook --------------------------------------------------------------------------


def text_fields(form) -> dict[str, str] | None:
    """The posted fields as a plain dict, or None if any is a file: Twilio never sends one, so it can't be Twilio."""
    fields = dict(form.items())
    return fields if all(isinstance(value, str) for value in fields.values()) else None


def signature_is_valid(settings: Settings, fields: dict[str, str], signature: str | None) -> bool:
    if not signature:
        return False
    # The address from settings, not the request's Host: the request arrives through a tunnel that changes the host
    # and the scheme, while the signature covers the address Twilio was given.
    url = settings.public_base_url.rstrip("/") + WEBHOOK_PATH
    return RequestValidator(settings.twilio_auth_token).validate(url, fields, signature)


def record_rejection(conn: sqlite3.Connection) -> None:
    # No content: not the body, the number or the id. A forged message is untrusted text, and the log is shown on
    # a page.
    audit.record(
        conn, at=clock.now(conn), family_id=None, actor="twilio", event="inbound.rejected", data={"channel": CHANNEL}
    )


# --- files sent with a message -------------------------------------------------------------------


def fetch_media(fields: dict[str, str], sid: str, token: str) -> tuple[list[tuple[str, bytes]], int]:
    """The photos and PDFs sent with a message as (media type, bytes), and how many files had to be left out."""
    try:
        count = int(fields.get("NumMedia", "0"))
    except ValueError:
        count = 0
    files: list[tuple[str, bytes]] = []
    skipped = 0
    if count < 1:
        return files, skipped
    # Twilio redirects the media URL to a CDN. httpx drops the login when the host changes, so the CDN never sees it.
    with httpx.Client(auth=(sid, token), follow_redirects=True, timeout=MEDIA_TIMEOUT) as http:
        for index in range(count):
            media_type = fields.get(f"MediaContentType{index}", "")
            url = fields.get(f"MediaUrl{index}")
            usable = media_type.startswith("image/") or media_type == "application/pdf"
            data = _download(http, url) if usable and url else None
            if data is None:
                skipped += 1
            else:
                files.append((media_type, data))
    return files, skipped


def _download(http: httpx.Client, url: str) -> bytes | None:
    """The file, or None if it can't be fetched or is over the limit. Stops reading once the limit is passed."""
    limit = base.MAX_ATTACHMENT_BYTES
    try:
        with http.stream("GET", url) as response:
            response.raise_for_status()
            data = bytearray()
            for part in response.iter_bytes():
                data += part
                if len(data) > limit:
                    return None
            return bytes(data)
    except httpx.HTTPError as error:
        # The type only: the address names the account and the message.
        logger.warning("media download failed: %s", type(error).__name__)
        return None


def _skipped_note(count: int) -> str:
    return f"I couldn't use {count} {'file' if count == 1 else 'files'}. I can read photos and PDFs under 5 MB."


# --- sending -------------------------------------------------------------------------------------


def chunks(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """`text` in pieces of at most `size` characters, cut at whitespace where there is any."""
    pieces = []
    rest = text.strip()
    while len(rest) > size:
        cut = _cut_point(rest, size)
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces


def _cut_point(text: str, size: int) -> int:
    window = text[: size + 1]
    last_space = max(window.rfind(whitespace) for whitespace in (" ", "\n", "\t"))
    return last_space if last_space > 0 else size


def twilio_sender(sid: str, token: str, from_: str, *, http_client: HttpClient | None = None) -> Sender:
    # A timeout, because Twilio's own client has none and a stuck call would hold a worker for good.
    client = Client(sid, token, http_client=http_client or TwilioHttpClient(timeout=SEND_TIMEOUT_SECONDS))

    def send(to: str, body: str) -> None:
        client.messages.create(from_=from_, to=to, body=body)

    return send


def send_text(conn: sqlite3.Connection, sender: Sender, to: str, text: str, *, family_id: str | None) -> bool:
    """Sends `text` in as many messages as it needs. False, with the reason audited, if Twilio would not take one."""
    for piece in chunks(text):
        try:
            sender(to, piece)
        except (TwilioException, OSError) as error:
            # Only the kind of error and Twilio's code: the text may carry a confirm link, and the message of a
            # Twilio error names the account.
            audit.record(
                conn,
                at=clock.now(conn),
                family_id=family_id,
                actor=CHANNEL,
                event="whatsapp.send_failed",
                data={"error": type(error).__name__, "code": getattr(error, "code", None)},
            )
            return False
    return True


def deliver_pending(conn: sqlite3.Connection, sender: Sender, family_id: str) -> None:
    """Sends the WhatsApp rows waiting in the outbox for this family's members, and marks each one that went out.

    A row Twilio refused stays unsent and is tried again the next time. Its member's later rows wait behind it, so
    messages never arrive out of order.
    """
    with _DELIVERY:
        for member in store.members(conn, family_id):
            addresses = [address for channel, address in store.identities_of(conn, member.id) if channel == CHANNEL]
            if not addresses:
                continue
            for row in outbox.pending(conn, member.id, CHANNEL):
                if not send_text(conn, sender, addresses[0], row["text"], family_id=family_id):
                    break
                outbox.mark_delivered(conn, [row["id"]], clock.now(conn))


def deliver_all(conn: sqlite3.Connection, sender: Sender) -> None:
    """For messages written with no chat or PIN page open to carry them: a payment notice, the scheduler."""
    for family in conn.execute("SELECT id FROM families ORDER BY rowid").fetchall():
        deliver_pending(conn, sender, family["id"])


# The two below are for background tasks, which must not use the request's connection.


def deliver_for_family(settings: Settings, sender: Sender, family_id: str) -> None:
    with closing(db.connect(settings.db_path)) as conn:
        deliver_pending(conn, sender, family_id)


def deliver_everywhere(settings: Settings, sender: Sender) -> None:
    with closing(db.connect(settings.db_path)) as conn:
        deliver_all(conn, sender)


# --- one incoming message ------------------------------------------------------------------------


def process_inbound(settings: Settings, *, client, provider: Provider, sender: Sender, fields: dict[str, str]) -> None:
    """Runs after Twilio has its answer: reads the files, asks the agent, sends the reply, then the waiting messages."""
    attachments, skipped = fetch_media(fields, settings.twilio_account_sid, settings.twilio_auth_token)
    message = InboundMessage(CHANNEL, fields["MessageSid"], fields["From"], fields.get("Body", ""), attachments)
    with closing(db.connect(settings.db_path)) as conn:
        reply = base.handle(
            conn, message, client=client, provider=provider, base_url=settings.public_base_url, now=clock.now(conn)
        )
        member = store.member_for_identity(conn, CHANNEL, message.address)
        family_id = None if member is None else member.family_id
        if reply is not None:
            note = f"\n\n{_skipped_note(skipped)}" if skipped else ""
            send_text(conn, sender, message.address, reply + note, family_id=family_id)
        if family_id is not None:
            deliver_pending(conn, sender, family_id)
