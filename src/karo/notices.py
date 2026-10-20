"""Provider notices: a payment is believed only when the signature checks out AND the provider confirms it again.

The notice is a claim anyone with the URL can post. The re-check is Karo asking the provider directly, so a forged or
altered notice can't move money into the books.

The steps, in order:
  1. the notice is parsed (this is where the signature is checked);
  2. its event id is looked up, read-only, to turn a repeat away early;
  3. the collection is found by tx_ref;
  4. the provider is asked about the payment, and for the conversion price if one is needed. These are provider
     calls, so no transaction is open;
  5. one transaction saves the event id and writes the collection, the ledger, the share and the audit log;
  6. the main payer is told, after the commit.

The event id is saved only in step 5, with the credit. Saved earlier, a crash during the re-check would leave it behind,
and the provider's retry would be turned away as a duplicate with the payment never credited.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta

from karo import audit, db, ledger, outbox, store
from karo.clock import iso
from karo.money import Money
from karo.providers.base import CollectionStatus, Notice, Provider
from karo.throttle import RejectionThrottle

ALL_IN = "All shares are in. Ask me to pay the fees when you're ready."

# How often the scheduler asks about a payment link still pending, in case its notice was lost.
VERIFY_EVERY = timedelta(minutes=10)

# A flood of forged posts writes one audit row a minute, carrying how many there were.
REJECTION_WINDOW = timedelta(seconds=60)
# One window per database file and provider, so two apps (or two tests) in one process never share a window.
_REJECTIONS: dict[tuple[str, str], RejectionThrottle] = {}
_REJECTIONS_LOCK = threading.Lock()


def forget_rejections() -> None:
    """Closes every window. A demo reset deletes the database, so the old windows no longer describe it."""
    with _REJECTIONS_LOCK:
        _REJECTIONS.clear()


def _throttle(conn: sqlite3.Connection, provider_name: str) -> RejectionThrottle:
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    with _REJECTIONS_LOCK:
        return _REJECTIONS.setdefault((database, provider_name), RejectionThrottle(REJECTION_WINDOW))


def handle_notice(
    conn: sqlite3.Connection, provider: Provider, *, headers: Mapping[str, str], body: bytes, now: datetime
) -> str:
    """One of "credited", "rejected", "duplicate", "unknown" or "unverified"."""
    notice = provider.parse_notice(headers, body)
    if notice is None:
        count = _throttle(conn, provider.name).take(now)
        if count is not None:
            # The body is left out: it came from someone who couldn't prove who they are.
            audit.record(
                conn,
                at=now,
                family_id=None,
                actor="system",
                event="notice.rejected",
                # No count for a single post, which keeps the row as it always was.
                data={"provider": provider.name} | ({"count": count} if count > 1 else {}),
            )
        return "rejected"

    known = conn.execute(
        "SELECT 1 FROM provider_events WHERE provider = ? AND event_id = ?", (notice.provider, notice.event_id)
    ).fetchone()
    if known is not None:
        return "duplicate"
    return _confirm_and_credit(conn, provider, notice, now)


def _confirm_and_credit(conn: sqlite3.Connection, provider: Provider, notice: Notice, now: datetime) -> str:
    collection = None if notice.tx_ref is None else store.collection_by_tx_ref(conn, notice.tx_ref)
    if collection is None:
        return "unknown"

    reported = provider.verify_collection(collection["tx_ref"])
    local = Money(collection["amount_minor"], collection["currency"])
    if not _confirms(reported, local):
        audit.record(
            conn,
            at=now,
            family_id=collection["family_id"],
            actor="system",
            event="collection.unverified",
            data={
                "collection_id": collection["id"],
                "tx_ref": collection["tx_ref"],
                "status": reported.status,
                "reported": None if reported.amount is None else str(reported.amount),
                "expected": str(local),
            },
        )
        return "unverified"

    return _credit_confirmed(conn, provider, notice, collection, reported, now)


def settle(conn: sqlite3.Connection, provider: Provider, collection: sqlite3.Row, *, now: datetime) -> str:
    """Asks the provider about a pending payment link and credits it if it was paid. For a notice that never came.
    One of "credited", "duplicate" or "pending". Must not run inside a transaction: it calls the provider."""
    reported = provider.verify_collection(collection["tx_ref"])
    local = Money(collection["amount_minor"], collection["currency"])
    if _confirms(reported, local):
        # One event id per payment link, so this credit and a late notice for it go through the same duplicate checks.
        notice = Notice(provider.name, f"verify:{collection['tx_ref']}", collection["tx_ref"], None)
        return _credit_confirmed(conn, provider, notice, collection, reported, now)
    # Never marked failed here: a provider that doesn't know the link (the stand-in after a restart) may still be paid.
    with db.transaction(conn):
        conn.execute(
            "UPDATE collections SET updated_at = ? WHERE id = ? AND family_id = ? AND status = 'pending'",
            (iso(now), collection["id"], collection["family_id"]),
        )
    return "pending"


def _credit_confirmed(
    conn: sqlite3.Connection,
    provider: Provider,
    notice: Notice,
    collection: sqlite3.Row,
    reported: CollectionStatus,
    now: datetime,
) -> str:
    local = Money(collection["amount_minor"], collection["currency"])
    credit = Money(collection["credit_minor"], collection["credit_currency"])
    home = store.family_config(conn, collection["family_id"]).policy.home_currency
    converted = None if local.currency == home else provider.quote(home, credit).source
    with db.transaction(conn):
        told = _credit(conn, notice, collection, reported, local, credit, converted, now)
    if told is None:
        return "duplicate"
    outbox.notify(conn, store.main_payer(conn, collection["family_id"]).id, told, now)
    return "credited"


def _confirms(reported: CollectionStatus, local: Money) -> bool:
    # At least the amount: a payer who covered the provider's fee too has still paid.
    return (
        reported.status == "succeeded"
        and reported.amount is not None
        and reported.amount.currency == local.currency
        and reported.amount.minor >= local.minor
    )


def _credit(
    conn: sqlite3.Connection,
    notice: Notice,
    collection: sqlite3.Row,
    reported: CollectionStatus,
    local: Money,
    credit: Money,
    converted: Money | None,
    now: datetime,
) -> str | None:
    """Writes the payment. Returns what to tell the main payer, or None if this notice or collection is done already."""
    family_id, collection_id = collection["family_id"], collection["id"]
    first = conn.execute(
        "INSERT OR IGNORE INTO provider_events (provider, event_id, received_at) VALUES (?, ?, ?)",
        (notice.provider, notice.event_id, iso(now)),
    )
    if first.rowcount == 0:
        # A copy of this notice got here between the early look and now.
        return None
    moved = conn.execute(
        "UPDATE collections SET status = 'succeeded', provider_id = ?, updated_at = ? "
        "WHERE id = ? AND family_id = ? AND status = 'pending'",
        (reported.provider_id, iso(now), collection_id, family_id),
    )
    if moved.rowcount == 0:
        # Another notice, under a different event id, got here first. This id is still saved, so a replay stops early.
        return None

    ledger.post(conn, family_id=family_id, at=now, amount=local, kind="collection", ref=collection_id)
    share = store.get_share(conn, family_id, collection["share_id"])
    audit.record(
        conn,
        at=now,
        family_id=family_id,
        actor="system",
        event="collection.credited",
        data={
            "collection_id": collection_id,
            "share_id": share["id"],
            "member_id": share["member_id"],
            "tx_ref": collection["tx_ref"],
            "amount": str(local),
            "credit": str(credit),
            "provider_id": reported.provider_id,
        },
    )
    if converted is not None:
        out_of_local = Money(-local.minor, local.currency)
        ledger.post(conn, family_id=family_id, at=now, amount=out_of_local, kind="conversion", ref=collection_id)
        ledger.post(conn, family_id=family_id, at=now, amount=converted, kind="conversion", ref=collection_id)
        audit.record(
            conn,
            at=now,
            family_id=family_id,
            actor="system",
            event="conversion.auto",
            data={"collection_id": collection_id, "from": str(local), "to": str(converted), "simulated": True},
        )

    conn.execute(
        "UPDATE shares SET paid_minor = paid_minor + ? WHERE id = ? AND family_id = ?",
        (credit.minor, share["id"], family_id),
    )
    payer = store.member(conn, family_id, share["member_id"])
    text = f"{payer.name} has paid their share ({credit})"
    if _completes_the_fee(conn, family_id, share):
        audit.record(
            conn,
            at=now,
            family_id=family_id,
            actor="system",
            event="fee.collected",
            data={"fee_id": share["fee_id"]},
        )
        text += f". {ALL_IN}"
    return text


def _completes_the_fee(conn: sqlite3.Connection, family_id: str, before: sqlite3.Row) -> bool:
    """True if this payment is the one that filled the last open share of the fee."""
    if before["fee_id"] is None or before["paid_minor"] >= before["amount_minor"]:
        return False
    shares = store.shares_for_fee(conn, family_id, before["fee_id"])
    return all(s["paid_minor"] >= s["amount_minor"] for s in shares)
