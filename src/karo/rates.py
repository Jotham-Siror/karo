"""The market (mid-market) exchange rate, so a provider's rate can be shown next to what the market says.

It is a reference only: nothing here decides or moves money, so a lookup that fails just returns None.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from karo import clock, db

# The free service's terms ask for this wherever its rates are shown.
ATTRIBUTION = "Rates by Exchange Rate API"

_URL = "https://open.er-api.com/v6/latest/{source}"
_TIMEOUT_SECONDS = 5.0
_FRESH_FOR = timedelta(hours=12)


class _UnusableReply(Exception):
    """The service answered, but not with a rate we can use."""


def mid_market(conn: sqlite3.Connection, source_currency: str, target_currency: str, now: datetime) -> Decimal | None:
    """Units of `target_currency` for one unit of `source_currency`, or None when no rate can be had.

    A rate fetched in the last 12 hours is reused. When the service can't be reached or answers nonsense, the last
    rate we saw is used however old it is: a slightly stale reference is better than none."""
    cached = _cached(conn, source_currency)
    if cached is not None and now - cached[0] < _FRESH_FOR:
        return _rate_in(cached[1], target_currency)
    try:
        rates, rate = _fetch(source_currency, target_currency)
    except (httpx.HTTPError, _UnusableReply):
        return _rate_in(cached[1], target_currency) if cached is not None else None
    _save(conn, source_currency, rates, now)
    return rate


def _fetch(source_currency: str, target_currency: str) -> tuple[dict, Decimal]:
    response = httpx.get(_URL.format(source=source_currency), timeout=_TIMEOUT_SECONDS)
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError as exc:  # not JSON, or not text at all
        raise _UnusableReply("not JSON") from exc
    if not isinstance(body, dict) or body.get("result") != "success" or not isinstance(body.get("rates"), dict):
        raise _UnusableReply("not a successful rates reply")
    rate = _rate_in(body["rates"], target_currency)
    if rate is None:
        raise _UnusableReply(f"no usable rate for {target_currency}")
    return body["rates"], rate


def _rate_in(rates: dict, currency: str) -> Decimal | None:
    try:
        rate = Decimal(str(rates[currency]))
    except (KeyError, InvalidOperation):
        return None
    return rate if rate.is_finite() and rate > 0 else None


def _cached(conn: sqlite3.Connection, source_currency: str) -> tuple[datetime, dict] | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (f"rates:{source_currency}",)).fetchone()
    if row is None:
        return None
    saved = json.loads(row["value"])
    return clock.parse(saved["fetched_at"]), saved["rates"]


def _save(conn: sqlite3.Connection, source_currency: str, rates: dict, now: datetime) -> None:
    value = json.dumps({"fetched_at": clock.iso(now), "rates": rates})
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"rates:{source_currency}", value),
        )
