"""Provider notices: a payment is believed only when the signature checks out AND the provider confirms it again.

The notice is a claim anyone with the URL can post. The re-check is Karo asking the provider directly, so a forged or
altered notice can't move money into the books.

The steps, in order:
  1. the notice is parsed (this is where the signature is checked);
  2. its event id is saved, so a repeat is recognised;
  3. the collection is found by tx_ref;
  4. the provider is asked about the payment, and for the conversion price if one is needed. These are provider
     calls, so no transaction is open;
  5. one transaction writes the collection, the ledger, the share and the audit log;
  6. the main payer is told, after the commit.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime

from karo import audit, db, ledger, outbox, store
from karo.clock import iso
from karo.money import Money
from karo.providers.base import CollectionStatus, Notice, Provider

ALL_IN = "All shares are in. Ask me to pay the fees when you're ready."


def handle_notice(
    conn: sqlite3.Connection, provider: Provider, *, headers: Mapping[str, str], body: bytes, now: datetime
) -> str:
    """One of "credited", "rejected", "duplicate", "unknown" or "unverified"."""
    notice = provider.parse_notice(headers, body)
    if notice is None:
        # The body is left out: it came from someone who couldn't prove who they are.
        audit.record(
            conn, at=now, family_id=None, actor="system", event="notice.rejected", data={"provider": provider.name}
        )
        return "rejected"

    first = conn.execute(
        "INSERT OR IGNORE INTO provider_events (provider, event_id, received_at) VALUES (?, ?, ?)",
        (notice.provider, notice.event_id, iso(now)),
    )
    if first.rowcount == 0:
        return "duplicate"
    try:
        result = _confirm_and_credit(conn, provider, notice, now)
    except BaseException:
        _forget(conn, notice)
        raise
    if result == "unverified":
        _forget(conn, notice)
    return result


def _forget(conn: sqlite3.Connection, notice: Notice) -> None:
    # A payment that couldn't be confirmed yet must not use up its event id: the provider's retry is the only
    # other chance to credit it, and it would be turned away as a duplicate.
    conn.execute("DELETE FROM provider_events WHERE provider = ? AND event_id = ?", (notice.provider, notice.event_id))


def _confirm_and_credit(conn: sqlite3.Connection, provider: Provider, notice: Notice, now: datetime) -> str:
    collection = None if notice.tx_ref is None else store.collection_by_tx_ref(conn, notice.tx_ref)
    if collection is None:
        return "unknown"

    seen = provider.verify_collection(collection["tx_ref"])
    local = Money(collection["amount_minor"], collection["currency"])
    if not _confirms(seen, local):
        audit.record(
            conn,
            at=now,
            family_id=collection["family_id"],
            actor="system",
            event="collection.unverified",
            data={
                "collection_id": collection["id"],
                "tx_ref": collection["tx_ref"],
                "status": seen.status,
                "reported": None if seen.amount is None else str(seen.amount),
                "expected": str(local),
            },
        )
        return "unverified"

    credit = Money(collection["credit_minor"], collection["credit_currency"])
    home = store.family_config(conn, collection["family_id"]).policy.home_currency
    converted = None if local.currency == home else provider.quote(home, credit).source
    with db.transaction(conn):
        told = _credit(conn, collection, seen, local, credit, converted, now)
    if told is None:
        return "duplicate"
    outbox.notify(conn, store.main_payer(conn, collection["family_id"]).id, told, now)
    return "credited"


def _confirms(seen: CollectionStatus, local: Money) -> bool:
    # At least the amount: a payer who covered the provider's fee too has still paid.
    return (
        seen.status == "succeeded"
        and seen.amount is not None
        and seen.amount.currency == local.currency
        and seen.amount.minor >= local.minor
    )


def _credit(
    conn: sqlite3.Connection,
    collection: sqlite3.Row,
    seen: CollectionStatus,
    local: Money,
    credit: Money,
    converted: Money | None,
    now: datetime,
) -> str | None:
    """Writes the payment. Returns what to tell the main payer, or None if this collection was credited already."""
    family_id, collection_id = collection["family_id"], collection["id"]
    moved = conn.execute(
        "UPDATE collections SET status = 'succeeded', provider_id = ?, updated_at = ? "
        "WHERE id = ? AND family_id = ? AND status = 'pending'",
        (seen.provider_id, iso(now), collection_id, family_id),
    )
    if moved.rowcount == 0:
        # Another notice (under a different event id) got here first.
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
            "provider_id": seen.provider_id,
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
