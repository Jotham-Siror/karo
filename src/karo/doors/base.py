"""What every channel does with an incoming message: dedupe it, find who sent it, and hand it to the agent.

The web page and WhatsApp differ only in how they get a message in and a reply out.
"""

from __future__ import annotations

import base64
import logging
import re
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from karo import audit, proposals, store
from karo.agent import loop
from karo.agent.claude import Claude
from karo.clock import iso
from karo.providers.base import Provider

logger = logging.getLogger(__name__)

FREEZE_WORDS = {"stop", "freeze", "simamisha"}

UNKNOWN_SENDER = "Sorry, this number isn't linked to a Karo family."
PIN_IN_CHAT = "Never type your PIN in chat. Karo will never ask for it. Use only the PIN page."
AGENT_ERROR = "Sorry, something went wrong on my side. Please try again in a minute."
NOTHING_TO_READ = "Please type a message or send a photo or PDF."
CANNOT_READ_FILE = "Sorry, I can only read photos (JPEG, PNG, GIF or WebP) and PDFs."
FILE_TOO_BIG = "Sorry, that file is too big. Please send files under 5 MB."
TOO_MANY_FILES = "Sorry, please send up to 5 files at a time."
TOO_MANY_TURNS = "That's a lot of messages. Please wait a few minutes and try again."

MAX_TURNS_PER_HOUR = 30
_TURN_WINDOW = timedelta(hours=1)
# When each member's recent turns were handled. In this process only, so a restart forgets them; tests clear it.
RECENT_TURNS: dict[str, deque[datetime]] = {}
_turns_lock = threading.Lock()

# The API refuses images over 5 MB.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENTS = 5

_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_PDF_TYPE = "application/pdf"
# The shape approvals accepts as a PIN. [0-9], not \d, which would also match digits no PIN can contain.
PIN_SHAPE = re.compile(r"[0-9]{4,6}")
_PIN_WORD = re.compile(r"\bpin\b", re.IGNORECASE)
_PIN_NUMBER = re.compile(r"\b[0-9]{4,6}\b")


@dataclass(frozen=True)
class InboundMessage:
    channel: str
    message_id: str
    address: str
    text: str
    attachments: list[tuple[str, bytes]] = field(default_factory=list)  # (media type, bytes)


def handle(
    conn: sqlite3.Connection,
    msg: InboundMessage,
    *,
    client: Claude,
    provider: Provider,
    base_url: str,
    now: datetime,
) -> str | None:
    """The reply to send back, or None for a message already handled (a retry from the channel)."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO inbound_messages (channel, message_id, received_at) VALUES (?, ?, ?)",
        (msg.channel, msg.message_id, iso(now)),
    )
    if cursor.rowcount == 0:
        return None

    member = store.member_for_identity(conn, msg.channel, msg.address)
    if member is None:
        return UNKNOWN_SENDER

    text = msg.text.strip()
    if _looks_like_a_typed_pin(text):
        # Not stored, not shown to the agent, and left out of the log: the audit row records only that it happened.
        audit.record(
            conn,
            at=now,
            family_id=member.family_id,
            actor=member.id,
            event="pin.typed_in_chat",
            data={"channel": msg.channel},
        )
        return PIN_IN_CHAT
    if text.lower() in FREEZE_WORDS:
        return proposals.freeze(conn, family_id=member.family_id, actor=member, now=now)

    problem = _attachment_problem(msg.attachments)
    if problem is not None:
        return problem
    content = _content_blocks(text, msg.attachments)
    if not content:
        return NOTHING_TO_READ
    if not _take_a_turn(member.id, now):
        return TOO_MANY_TURNS
    try:
        return loop.run_turn(
            conn, client, provider, member=member, channel=msg.channel, content=content, now=now, base_url=base_url
        )
    except Exception as error:
        # Network, model and payment-provider failures all end here: the person gets a sorry, never a stack trace.
        logger.exception("agent turn failed")
        audit.record(
            conn,
            at=now,
            family_id=member.family_id,
            actor=member.id,
            event="agent.error",
            data={"error": type(error).__name__},
        )
        return AGENT_ERROR


def _looks_like_a_typed_pin(text: str) -> bool:
    # The digits are never compared with the real PIN: that would let anyone guess it in chat, around the lockout.
    return bool(PIN_SHAPE.fullmatch(text) or (_PIN_WORD.search(text) and _PIN_NUMBER.search(text)))


def _take_a_turn(member_id: str, now: datetime) -> bool:
    """Counts a turn for the member and returns True, or returns False at the limit. Refused turns are not counted."""
    with _turns_lock:
        turns = RECENT_TURNS.setdefault(member_id, deque())
        while turns and turns[0] <= now - _TURN_WINDOW:
            turns.popleft()
        if len(turns) >= MAX_TURNS_PER_HOUR:
            return False
        turns.append(now)
        return True


def _attachment_problem(attachments: list[tuple[str, bytes]]) -> str | None:
    """The reply to give instead of running the agent, when a file can't be used. None if they are all fine."""
    if len(attachments) > MAX_ATTACHMENTS:
        return TOO_MANY_FILES
    for media_type, data in attachments:
        if media_type not in _IMAGE_TYPES and media_type != _PDF_TYPE:
            return CANNOT_READ_FILE
        if len(data) > MAX_ATTACHMENT_BYTES:
            return FILE_TOO_BIG
    return None


def _content_blocks(text: str, attachments: list[tuple[str, bytes]]) -> list[dict]:
    """Anthropic content blocks, files first, since the API reads best with the images ahead of the question."""
    blocks = [
        {
            "type": "document" if media_type == _PDF_TYPE else "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode("ascii")},
        }
        for media_type, data in attachments
    ]
    if text:
        blocks.append({"type": "text", "text": text})
    return blocks
