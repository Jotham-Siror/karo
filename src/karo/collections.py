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


def _fail(conn: sqlite3.Connection, old: sqlite3.Row, now: datetime) -> None:
    conn.execute(
        "UPDATE collections SET status = 'failed', updated_at = ? "
        "WHERE id = ? AND family_id = ? AND status = 'pending'",
        (iso(now), old["id"], old["family_id"]),
    )
    audit.record(
        conn,
        at=now,
        family_id=old["family_id"],
        actor="system",
        event="collection.failed",
        data={"collection_id": old["id"], "share_id": old["share_id"], "reason": "The provider lost this link."},
    )


def request_share(
    conn: sqlite3.Connection, provider: Provider, *, family_id: str, share_id: str, now: datetime, base_url: str
) -> str | None:
    """Asks the share's owner to pay what is left of it. Returns the collection id, or None if nothing was asked.

    Nothing is asked when the rules block it, when nothing is left to pay, or when the share already has a live link.
    A link is live unless the provider says it has failed (the stand-in says so for one it has forgotten, as after a
    restart). A dead one is marked failed as the new one is saved, so a share never holds two live links.
    """
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

    pending = store.pending_collections(conn, family_id, share_id)
    if any(provider.verify_collection(old["tx_ref"]).status != "failed" for old in pending):
        return None

    contact = store.member_contact(conn, family_id, payer.id)
    country = countries.get(contact["country"])
    if not country.collect_methods:
        raise ValueError(f"Karo can't collect payments in {country.name} yet.")
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
        for old in pending:
            _fail(conn, old, now)
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
    """Asks for every share of the fee that has something left to pay and no live link. Returns how many were asked.

    Safe to run again after a partial failure: a share that already has a live link is left alone."""
    asked = 0
    for share in store.shares_for_fee(conn, family_id, fee_id):
        collection_id = request_share(
            conn, provider, family_id=family_id, share_id=share["id"], now=now, base_url=base_url
        )
        if collection_id is not None:
            asked += 1
    return asked


def _shares_to_remind(
    conn: sqlite3.Connection, family_id: str, fee_id: str, share_id: str | None
) -> list[sqlite3.Row]:
    return [s for s in store.shares_for_fee(conn, family_id, fee_id) if share_id is None or s["id"] == share_id]


def send_reminders(
    conn: sqlite3.Connection,
    provider: Provider,
    *,
    family_id: str,
    fee_id: str,
    now: datetime,
    base_url: str,
    actor: str = "system",
    share_id: str | None = None,
) -> int:
    """Reminds each member who still owes on the fee. Returns how many people were messaged. With `share_id`, only
    that share's owner is reminded.

    A share with no live link is given a new one first (its own message is the notice, so it gets no second one).
    That is a provider call, so this must not run inside a transaction."""
    if conn.in_transaction:
        raise RuntimeError("send_reminders must not run inside a transaction.")
    fee = store.get_fee(conn, family_id, fee_id)
    if fee is None:
        return 0
    fresh = {
        share["id"]
        for share in _shares_to_remind(conn, family_id, fee_id, share_id)
        if request_share(conn, provider, family_id=family_id, share_id=share["id"], now=now, base_url=base_url)
        is not None
    }
    sent = 0
    with db.transaction(conn):
        for share in _shares_to_remind(conn, family_id, fee_id, share_id):
            remaining = _remaining(share)
            pending = store.latest_pending_collection(conn, family_id, share["id"])
            if remaining.minor <= 0 or pending is None:
                continue
            if share["id"] in fresh:
                sent += 1
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
