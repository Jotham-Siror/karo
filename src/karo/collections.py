"""Money in: asking each relative for their share through the provider's payment link, and reminding them.

A provider call is never made inside a transaction. A request reads and checks first, then calls the provider,
then commits the collection row, its audit entry and the message together.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from karo import audit, countries, db, outbox, proposals, rules, store
from karo.clock import iso
from karo.domain import CheckContext, Kind, Proposal, Stage
from karo.money import Money
from karo.providers.base import Provider


def _remaining(share: sqlite3.Row) -> Money:
    return Money(share["amount_minor"] - share["paid_minor"], share["currency"])


def _blocked(
    conn: sqlite3.Connection, family_id: str, share_id: str, member_id: str | None, reason: str, now: datetime
) -> None:
    audit.record(
        conn,
        at=now,
        family_id=family_id,
        actor="system",
        event="collection.blocked",
        data={"share_id": share_id, "member_id": member_id, "reason": reason},
    )


def request_share(
    conn: sqlite3.Connection, provider: Provider, *, family_id: str, share_id: str, now: datetime, base_url: str
) -> str | None:
    """Asks the share's owner to pay what is left of it. Returns the collection id, or None if nothing was asked."""
    if conn.in_transaction:
        raise RuntimeError("request_share must not run inside a transaction.")
    share = store.get_share(conn, family_id, share_id)
    if share is None:
        _blocked(conn, family_id, share_id, None, "There is no such share.", now)
        return None
    remaining = _remaining(share)
    if remaining.minor <= 0:
        return None

    payer = store.member(conn, family_id, share["member_id"])
    proposal = Proposal(
        id=db.new_id("prop"),
        family_id=family_id,
        kind=Kind.CONTRIBUTE,
        requested_by=payer.id,
        amount=remaining,
        share_id=share_id,
    )
    decision = rules.check(proposal, store.load_state(conn, family_id, now), CheckContext(now, Stage.PROPOSE, payer))
    if not decision.allowed:
        _blocked(conn, family_id, share_id, payer.id, proposals.block_reasons(decision), now)
        return None

    contact = store.member_contact(conn, family_id, payer.id)
    country = countries.get(contact["country"])
    method = country.collect_methods[0]
    quote = provider.quote(country.currency, remaining)
    collection_id = db.new_id("col")
    tx_ref = f"karo-{collection_id}"
    created = provider.create_collection(
        tx_ref=tx_ref,
        amount=quote.source,
        method=method.id,
        payer_name=contact["name"],
        payer_email=contact["email"] or f"{payer.id}@karo.example",
        payer_phone=contact["phone"],
        redirect_url=f"{base_url}/paid",
    )

    fee = None if share["fee_id"] is None else store.get_fee(conn, family_id, share["fee_id"])
    what = "the fees" if fee is None else f"the {fee['payee_name']} fee"
    stamp = iso(now)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO collections (id, share_id, family_id, provider, tx_ref, link, amount_minor, currency, "
            "credit_minor, credit_currency, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                collection_id,
                share_id,
                family_id,
                provider.name,
                tx_ref,
                created.link,
                quote.source.minor,
                quote.source.currency,
                remaining.minor,
                remaining.currency,
                stamp,
                stamp,
            ),
        )
        audit.record(
            conn,
            at=now,
            family_id=family_id,
            actor="system",
            event="collection.requested",
            data={
                "collection_id": collection_id,
                "share_id": share_id,
                "member_id": payer.id,
                "tx_ref": tx_ref,
                "amount": str(quote.source),
                "credit": str(remaining),
                "method": method.id,
            },
        )
        text = f"Hi {payer.name}, your share of {what} is {remaining}. Pay {quote.source} by {method.label}"
        outbox.notify(conn, payer.id, f"{text} here: {created.link}", now)
    return collection_id


def request_all(
    conn: sqlite3.Connection, provider: Provider, *, family_id: str, fee_id: str, now: datetime, base_url: str
) -> int:
    """Asks for every share of the fee that still has something to pay. Returns how many were asked."""
    asked = 0
    for share in store.shares_for_fee(conn, family_id, fee_id):
        collection_id = request_share(
            conn, provider, family_id=family_id, share_id=share["id"], now=now, base_url=base_url
        )
        if collection_id is not None:
            asked += 1
    return asked


def send_reminders(
    conn: sqlite3.Connection, *, family_id: str, fee_id: str, now: datetime, actor: str = "system"
) -> int:
    """Reminds each member who still owes on the fee, with their newest unpaid link. Returns how many were sent."""
    fee = store.get_fee(conn, family_id, fee_id)
    if fee is None:
        return 0
    sent = 0
    with db.transaction(conn):
        for share in store.shares_for_fee(conn, family_id, fee_id):
            remaining = _remaining(share)
            pending = store.latest_pending_collection(conn, family_id, share["id"])
            if remaining.minor <= 0 or pending is None:
                continue
            outbox.notify(
                conn,
                share["member_id"],
                f"Reminder: your share {remaining} for {fee['payee_name']} is still due. Pay here: {pending['link']}",
                now,
            )
            audit.record(
                conn,
                at=now,
                family_id=family_id,
                actor=actor,
                event="reminder.sent",
                data={"fee_id": fee_id, "share_id": share["id"], "member_id": share["member_id"]},
            )
            sent += 1
    return sent
