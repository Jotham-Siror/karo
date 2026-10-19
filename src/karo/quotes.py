"""Asking the provider what a payment costs, and keeping the answer for as long as the family allows."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from karo import audit, clock, db, store
from karo.domain import Quote
from karo.money import Money
from karo.providers.base import Provider


def create_quote(
    conn: sqlite3.Connection,
    provider: Provider,
    *,
    family_id: str,
    source_currency: str,
    target: Money,
    now: datetime,
) -> Quote:
    """The family pays `source_currency` and the payee gets exactly `target`. Good for the family's quote_ttl."""
    config = store.family_config(conn, family_id)
    # Asked before the transaction opens: a provider call may be slow, and the database must not stay locked for it.
    offered = provider.quote(source_currency, target)
    with db.transaction(conn):
        quote = store.save_quote(
            conn,
            family_id=family_id,
            provider=offered.provider,
            source=offered.source,
            target=offered.target,
            rate=offered.rate,
            created_at=now,
            expires_at=now + config.quote_ttl,
        )
        audit.record(
            conn,
            at=now,
            family_id=family_id,
            actor="system",
            event="quote.created",
            data={
                "quote_id": quote.id,
                "provider": offered.provider,
                "source": str(quote.source),
                "target": str(quote.target),
                "rate": str(offered.rate),
                "expires_at": clock.iso(quote.expires_at),
            },
        )
    return quote
