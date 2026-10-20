"""The main payer's PIN, the confirm links that carry her to the PIN page, and the confirm step itself.

A correct PIN writes a Confirmation holding the digest stored with the proposal. The rule checker accepts
it at execute time only while that digest still matches the proposal, so an edit after approval voids it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from karo import audit, clock, db, outbox, store
from karo.domain import Confirmation

_HASHER = PasswordHasher()
# fullmatch on [0-9]: `^\d{4,6}$` would also accept "1234\n" and non-ASCII digits.
_PIN_FORMAT = re.compile(r"[0-9]{4,6}")

USED_LINK_READABLE_FOR = timedelta(hours=24)

ConfirmStatus = Literal["confirmed", "wrong_pin", "locked", "invalid_link", "not_awaiting"]


@dataclass(frozen=True)
class PinResult:
    ok: bool
    locked_until: datetime | None
    attempts_left: int


@dataclass(frozen=True)
class LinkInfo:
    proposal_id: str
    member_id: str
    family_id: str


@dataclass(frozen=True)
class UsedLink:
    proposal_id: str
    family_id: str
    used_at: datetime


@dataclass(frozen=True)
class ConfirmResult:
    status: ConfirmStatus
    proposal_id: str | None


# --- the PIN -----------------------------------------------------------------------------------


def _peppered(pin: str, pepper: str) -> str:
    # Argon2 sees only the keyed digest, so a stolen database alone gives nothing to brute-force offline.
    return hmac.new(pepper.encode(), pin.encode(), hashlib.sha256).hexdigest()


def hash_pin(pin: str, pepper: str) -> str:
    if not _PIN_FORMAT.fullmatch(pin):
        raise ValueError("A PIN is 4 to 6 digits.")
    return _HASHER.hash(_peppered(pin, pepper))


def _pin_matches(stored: str | None, pin: str, pepper: str) -> bool:
    if stored is None:  # no PIN set yet: nothing can match, and the guess still counts toward the lockout
        return False
    try:
        return _HASHER.verify(stored, _peppered(pin, pepper))
    except VerifyMismatchError:
        return False


def set_pin(conn: sqlite3.Connection, family_id: str, pin: str, pepper: str, now: datetime) -> None:
    pin_hash = hash_pin(pin, pepper)
    with db.transaction(conn):
        cursor = conn.execute("UPDATE families SET pin_hash = ? WHERE id = ?", (pin_hash, family_id))
        if cursor.rowcount == 0:
            raise LookupError(f"There is no family {family_id!r}.")
        audit.record(conn, at=now, family_id=family_id, actor="system", event="pin.set", data={})


def _log_pin(conn: sqlite3.Connection, now: datetime, family_id: str, member_id: str, event: str, **data) -> None:
    audit.record(conn, at=now, family_id=family_id, actor=member_id, event=event, data={"member_id": member_id, **data})


def verify_pin(
    conn: sqlite3.Connection, family_id: str, member_id: str, pin: str, pepper: str, now: datetime
) -> PinResult:
    """Checks `pin` for the family's main payer. Wrong guesses are counted and lock the family out for a while."""
    with db.transaction(conn):
        row = store.family_row(conn, family_id)
        config = store.family_config(conn, family_id)
        if store.main_payer(conn, family_id).id != member_id:
            # Nobody else's guesses count: a relative must not be able to lock the main payer out.
            return PinResult(False, None, 0)
        if row["pin_locked_until"] is not None:
            locked_until = clock.parse(row["pin_locked_until"])
            if now < locked_until:
                return PinResult(False, locked_until, 0)

        if _pin_matches(row["pin_hash"], pin, pepper):
            conn.execute("UPDATE families SET pin_failures = 0, pin_locked_until = NULL WHERE id = ?", (family_id,))
            _log_pin(conn, now, family_id, member_id, "pin.ok")
            return PinResult(True, None, config.pin_max_attempts)

        failures = row["pin_failures"] + 1
        if failures >= config.pin_max_attempts:
            locked_until = now + config.pin_lockout
            conn.execute(
                "UPDATE families SET pin_failures = 0, pin_locked_until = ? WHERE id = ?",
                (clock.iso(locked_until), family_id),
            )
            _log_pin(
                conn, now, family_id, member_id, "pin.locked", failures=failures, locked_until=clock.iso(locked_until)
            )
            # Same format as proposals.when_text, which can't be imported here: proposals imports approvals.
            unlock = locked_until.astimezone(config.timezone).strftime("%a %d %b, %H:%M %Z")
            outbox.notify(
                conn,
                member_id,
                f"Someone entered the wrong PIN too many times, so approving payments is locked until {unlock}. "
                "If this wasn't you, reply FREEZE.",
                now,
            )
            return PinResult(False, locked_until, 0)

        attempts_left = config.pin_max_attempts - failures
        conn.execute("UPDATE families SET pin_failures = ? WHERE id = ?", (failures, family_id))
        _log_pin(conn, now, family_id, member_id, "pin.failed", failures=failures, attempts_left=attempts_left)
        return PinResult(False, None, attempts_left)


# --- confirm links -----------------------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_link(
    conn: sqlite3.Connection,
    *,
    family_id: str,
    proposal_id: str,
    member_id: str,
    now: datetime,
    base_url: str,
    ttl: timedelta,
) -> str:
    """Makes a one-time link and returns its URL, which carries the token.

    confirm_links keeps only the token's hash. The full URL is also in the main payer's outbox message, because
    that is how it reaches her; it must not go anywhere else (audit, logs, anything the AI sees).
    """
    token = secrets.token_urlsafe(32)
    with db.transaction(conn):
        if store.get_proposal(conn, family_id, proposal_id) is None:
            raise LookupError(f"Family {family_id!r} has no proposal {proposal_id!r}.")
        if store.member(conn, family_id, member_id) is None:
            raise LookupError(f"Family {family_id!r} has no member {member_id!r}.")
        conn.execute(
            "INSERT INTO confirm_links (token_hash, proposal_id, member_id, expires_at) VALUES (?, ?, ?, ?)",
            (_token_hash(token), proposal_id, member_id, clock.iso(now + ttl)),
        )
    return f"{base_url.rstrip('/')}/confirm/{token}"


def open_link(conn: sqlite3.Connection, token: str, now: datetime) -> LinkInfo | None:
    row = conn.execute(
        "SELECT l.proposal_id, l.member_id, l.expires_at, l.used_at, p.family_id "
        "FROM confirm_links l JOIN proposals p ON p.id = l.proposal_id WHERE l.token_hash = ?",
        (_token_hash(token),),
    ).fetchone()
    if row is None or row["used_at"] is not None or now >= clock.parse(row["expires_at"]):
        return None
    return LinkInfo(row["proposal_id"], row["member_id"], row["family_id"])


def used_link(conn: sqlite3.Connection, token: str, now: datetime) -> UsedLink | None:
    """A link whose PIN was already entered, so the page can say what became of it. That lasts a day after it was
    used, enough for a second press or a look back the next morning, and no longer: the URL lives on in chat
    history and logs. An unused, expired or unknown token gives nothing back. The family comes from the link's own
    member, so looking the proposal up with it can't show another family's."""
    row = conn.execute(
        "SELECT l.proposal_id, l.used_at, m.family_id FROM confirm_links l JOIN members m ON m.id = l.member_id "
        "WHERE l.token_hash = ? AND l.used_at IS NOT NULL",
        (_token_hash(token),),
    ).fetchone()
    if row is None:
        return None
    used_at = clock.parse(row["used_at"])
    if now >= used_at + USED_LINK_READABLE_FOR:
        return None
    return UsedLink(row["proposal_id"], row["family_id"], used_at)


def has_live_link(conn: sqlite3.Connection, family_id: str, proposal_id: str, now: datetime) -> bool:
    """True if a confirm link for this proposal can still be used."""
    rows = conn.execute(
        "SELECT l.expires_at FROM confirm_links l JOIN proposals p ON p.id = l.proposal_id "
        "WHERE l.proposal_id = ? AND p.family_id = ? AND l.used_at IS NULL",
        (proposal_id, family_id),
    )
    return any(now < clock.parse(row["expires_at"]) for row in rows)


# --- confirm -----------------------------------------------------------------------------------


def confirm(conn: sqlite3.Connection, *, token: str, pin: str, pepper: str, now: datetime) -> ConfirmResult:
    """Checks the link, the proposal and the PIN as one transaction, so two confirms can't interleave.

    A wrong PIN returns normally rather than raising: raising would roll back the failure count.
    The attempt that triggers the lockout reports "locked". A link made out to anyone but the main payer
    reports "wrong_pin" and counts nothing.
    """
    with db.transaction(conn):
        link = open_link(conn, token, now)
        if link is None:
            return ConfirmResult("invalid_link", None)
        _, proposal = store.get_proposal(conn, link.family_id, link.proposal_id)
        if proposal["status"] != "awaiting_pin":
            return ConfirmResult("not_awaiting", link.proposal_id)

        result = verify_pin(conn, link.family_id, link.member_id, pin, pepper, now)
        if not result.ok:
            return ConfirmResult("locked" if result.locked_until else "wrong_pin", link.proposal_id)

        # The digest stored when the proposal was made, not one recomputed now: the PIN approves what was shown.
        conn.execute(
            "INSERT INTO confirmations (proposal_id, member_id, digest, at) VALUES (?, ?, ?, ?)",
            (link.proposal_id, link.member_id, proposal["digest"], clock.iso(now)),
        )
        conn.execute("UPDATE confirm_links SET used_at = ? WHERE token_hash = ?", (clock.iso(now), _token_hash(token)))
        store.update_proposal(conn, link.family_id, link.proposal_id, status="confirmed", now=now)
        audit.record(
            conn,
            at=now,
            family_id=link.family_id,
            actor=link.member_id,
            event="proposal.confirmed",
            data={"proposal_id": link.proposal_id, "member_id": link.member_id, "digest": proposal["digest"]},
        )
        return ConfirmResult("confirmed", link.proposal_id)


def confirmation_for(conn: sqlite3.Connection, family_id: str, proposal_id: str) -> Confirmation | None:
    row = conn.execute(
        "SELECT c.proposal_id, c.member_id, c.digest, c.at FROM confirmations c "
        "JOIN proposals p ON p.id = c.proposal_id WHERE c.proposal_id = ? AND p.family_id = ?",
        (proposal_id, family_id),
    ).fetchone()
    if row is None:
        return None
    return Confirmation(row["proposal_id"], row["member_id"], row["digest"], clock.parse(row["at"]))
