"""Comparing ways to send a fee: the provider Karo can really use, against two worked examples.

The examples are illustrative figures from config/route_examples.json, not live quotes. Only the provider route can
be sent through Karo, so it is always the pick; the examples are there to show whether its price is fair.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from pathlib import Path

from karo import quotes, rates, store
from karo.money import Money, decimals
from karo.providers.base import Provider, payout_name

# src/karo/comparison.py -> repo root. Found from the package, so it works from any working directory.
_EXAMPLES_PATH = Path(__file__).resolve().parents[2] / "config" / "route_examples.json"

_PROVIDER_LABELS = {"flutterwave": "Flutterwave", "standin": "Stand-in provider (simulated)"}
_PROVIDER_SPEED = "about 1 day"
_BANK_WIRE = "Bank wire"


@dataclass(frozen=True)
class Option:
    name: str
    you_pay: Money
    they_receive: Money
    speed: str
    is_example: bool


@dataclass(frozen=True)
class Comparison:
    options: tuple[Option, ...]  # the provider route first, then the examples
    pick: Option
    line: str
    quote_id: str
    # How far above the market rate the provider's rate is, in percent to 2 places. Negative if it is below.
    # None when no market rate could be had.
    markup_pct: Decimal | None


def compare(
    conn: sqlite3.Connection, provider: Provider, *, family_id: str, target: Money, now: datetime
) -> Comparison:
    """Quotes paying `target` from the family's home currency, and sets it beside the examples. Saves that one quote."""
    source = store.family_config(conn, family_id).policy.home_currency
    quote = quotes.create_quote(conn, provider, family_id=family_id, source_currency=source, target=target, now=now)
    mid = rates.mid_market(conn, source, target.currency, now)
    pick = Option(_label(payout_name(provider)), quote.source, quote.target, _PROVIDER_SPEED, False)

    if mid is None:
        # With no market rate the examples are priced off the provider's own cost, so they still give a sense of scale.
        fair, markup = quote.source.decimal, None
    else:
        fair = target.decimal / mid
        markup = ((quote.source.decimal / fair - 1) * 100).quantize(Decimal("0.01"), ROUND_HALF_UP)

    examples = _examples(fair, target, source)
    return Comparison((pick, *examples), pick, _line(pick, examples, markup), quote.id, markup)


def _label(provider_name: str) -> str:
    return _PROVIDER_LABELS.get(provider_name, provider_name)


def _examples(fair: Decimal, target: Money, source: str) -> list[Option]:
    """What each example route would charge: the fair price in `source`, plus its markup, plus its fixed fee."""
    routes = json.loads(_EXAMPLES_PATH.read_text(encoding="utf-8"))["routes"]
    options = []
    for route in routes:
        fee = Money(route["fixed_fee"]["minor"], route["fixed_fee"]["currency"])
        if fee.currency != source:
            continue  # a fee in another currency can't be added to this family's price
        cost = fair * (1 + Decimal(route["markup_pct"]) / 100) + fee.decimal
        # Rounded up, like the provider's own quote, so an example is never shown cheaper than it would be.
        minor = cost.scaleb(decimals(source)).to_integral_value(ROUND_CEILING)
        options.append(Option(route["name"], Money(int(minor), source), target, route["speed"], True))
    return options


def _line(pick: Option, examples: list[Option], markup: Decimal | None) -> str:
    """One sentence for the agent to repeat word for word."""
    line = f"{pick.name}: you pay {pick.you_pay} and {pick.they_receive} arrives in about a day"
    if markup is None:
        # Without the market rate the examples are priced from the provider's own cost, so they always come out
        # dearer. Any saving claimed against them would be a claim against our own rate.
        return line + "."
    wire = next((o for o in examples if o.name.startswith(_BANK_WIRE)), None)
    if wire is not None:
        saving = wire.you_pay.minor - pick.you_pay.minor
        gap = Money(abs(saving), pick.you_pay.currency)
        line += f", {gap} {'less' if saving >= 0 else 'more'} than a typical bank wire (example figures)"
    side = "below" if markup < 0 else "above"
    return line + f"; its rate is {abs(markup)}% {side} the market rate ({rates.ATTRIBUTION})."
