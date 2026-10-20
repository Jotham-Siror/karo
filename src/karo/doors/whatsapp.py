"""The WhatsApp door, through Twilio: what comes in, what goes out, and the outbox rows that wait for a phone.

The web route (karo.web.app) checks the signature and answers Twilio at once. Everything slower than that, which is
the file downloads, the agent and the sending, runs here in a background task with its own database connection.
"""

from __future__ import annotations

import enum
import logging
import sqlite3
import threading
import weakref
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta

import httpx
from twilio.base.exceptions import TwilioException, TwilioRestException
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

# The file types doors.base reads (photos and PDFs). Another type would make it drop the words sent with the file,
# so the door skips such a file instead. tests/test_whatsapp.py checks that this list and doors.base agree.
READABLE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf"})

# After a failure that may pass (Twilio down, the network out), leave that member alone this long.
BACKOFF = timedelta(minutes=5)
# A flood of forged posts writes one audit row a minute, carrying how many there were.
REJECTION_WINDOW = timedelta(seconds=60)


class _DoorState:
    """What one running door remembers in memory. It is lost on a restart, which only means one more try."""

    def __init__(self) -> None:
        # Delivery reads the waiting rows and then marks them, so two at once would send twice. One at a time, and the
        # second does not wait: it would hold a worker thread behind a slow Twilio call. The next tick catches up.
        self.delivery = threading.Lock()
        self.backed_off_until: dict[str, datetime] = {}
        self.rejections = threading.Lock()
        self.last_rejection_row: datetime | None = None
        self.unrecorded_rejections = 0


# Kept with the sender it belongs to, so two apps (or two tests) never share a back-off. Dropped with the sender.
_STATES: weakref.WeakKeyDictionary[Sender, _DoorState] = weakref.WeakKeyDictionary()
_STATES_LOCK = threading.Lock()


def _state(sender: Sender) -> _DoorState:
    with _STATES_LOCK:
        if sender not in _STATES:
            _STATES[sender] = _DoorState()
        return _STATES[sender]


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


def record_rejection(conn: sqlite3.Connection, sender: Sender) -> None:
    """Counts a forged post, and writes an audit row for the count when a minute has passed since the last row.

    The posts held back since the last row are counted in the next one. No content in it: not the body, the number
    or the id. A forged message is untrusted text, and the log is shown on a page.
    """
    state = _state(sender)
    now = clock.now(conn)
    with state.rejections:
        state.unrecorded_rejections += 1
        if state.last_rejection_row is not None and now - state.last_rejection_row < REJECTION_WINDOW:
            return
        audit.record(
            conn,
            at=now,
            family_id=None,
            actor="twilio",
            event="inbound.rejected",
            data={"channel": CHANNEL, "count": state.unrecorded_rejections},
        )
        state.last_rejection_row = now
        state.unrecorded_rejections = 0


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
            data = _download(http, url) if media_type in READABLE_TYPES and url else None
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


class Result(enum.Enum):
    SENT = "sent"
    REFUSED = "refused"  # Twilio will never take it, so trying again is pointless
    FAILED = "failed"  # Twilio or the network is in trouble, and a later try may work


def _is_refusal(error: Exception) -> bool:
    # A 4xx is Twilio saying no to this message (outside the 24-hour window, a number that never joined the sandbox).
    # Not a rate limit or a timeout, which pass, nor a 401 or 403, which mean our own login is wrong or was changed.
    return (
        isinstance(error, TwilioRestException)
        and 400 <= error.status < 500
        and error.status not in (401, 403, 408, 429)
    )


def send_text(
    conn: sqlite3.Connection, sender: Sender, to: str, text: str, *, family_id: str | None, reply: bool = False
) -> Result:
    """Sends `text` in as many messages as it needs. If Twilio won't take one, it is audited and sending stops.

    A reply is never sent again; an outbox row is, after a failure that may pass.
    """
    for piece in chunks(text):
        try:
            sender(to, piece)
        except (TwilioException, OSError) as error:
            refused = _is_refusal(error)
            # Only the kind of error and Twilio's code: the text may carry a confirm link, and the message of a
            # Twilio error names the account.
            audit.record(
                conn,
                at=clock.now(conn),
                family_id=family_id,
                actor=CHANNEL,
                event="whatsapp.send_failed",
                data={
                    "error": type(error).__name__,
                    "code": getattr(error, "code", None),
                    "will_retry": not (refused or reply),
                },
            )
            return Result.REFUSED if refused else Result.FAILED
    return Result.SENT


def deliver_pending(conn: sqlite3.Connection, sender: Sender, family_id: str) -> None:
    """Sends the WhatsApp rows waiting in the outbox for this family's members, and marks each one that is finished.

    Skipped if another delivery is running; the next one catches up.
    """
    state = _state(sender)
    if not state.delivery.acquire(blocking=False):
        return
    try:
        for member in store.members(conn, family_id):
            _deliver_to(conn, sender, state, member.id, family_id)
    finally:
        state.delivery.release()


def _deliver_to(conn: sqlite3.Connection, sender: Sender, state: _DoorState, member_id: str, family_id: str) -> None:
    addresses = [address for channel, address in store.identities_of(conn, member_id) if channel == CHANNEL]
    backed_off_until = state.backed_off_until.get(member_id)
    if not addresses or (backed_off_until is not None and clock.now(conn) < backed_off_until):
        return
    for row in outbox.pending(conn, member_id, CHANNEL):
        result = send_text(conn, sender, addresses[0], row["text"], family_id=family_id)
        if result is Result.FAILED:
            # The rows behind it wait too, so messages never arrive out of order.
            state.backed_off_until[member_id] = clock.now(conn) + BACKOFF
            return
        # Sent, or refused for good. Either way this row is finished and the next one goes: a refused message loses
        # nothing, because the web copy of every message is always there.
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
            send_text(conn, sender, message.address, reply + note, family_id=family_id, reply=True)
        if family_id is not None:
            deliver_pending(conn, sender, family_id)
