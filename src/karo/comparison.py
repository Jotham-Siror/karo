"""Comparing ways to send a fee: the provider Karo can really use, against two worked examples.

The examples are illustrative figures from config/route_examples.json, not live quotes. Only the provider route can
be sent through Karo, so it is always the pick; the examples are there to show whether its price is fair.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path

from karo import quotes, rates, store
from karo.money import Money, decimals
from karo.providers.base import Provider, payout_name

# src/karo/comparison.py -> repo root. Found from the package, so it works from any working directory.
_EXAMPLES_PATH = Path(__file__).resolve().parents[2] / "config" / "route_examples.json"

_PROVIDER_LABELS = {"flutterwave": "Flutterwave", "standin": "Test payment (simulated, no real money)"}
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
    # What one unit of the target costs at the market rate, like "1 GBP = 172.41 KES". None with no market rate.
    market_rate: str | None


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
        fair, markup, market_rate = quote.source.decimal, None, None
    else:
        fair = target.decimal / mid
        markup = ((quote.source.decimal / fair - 1) * 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
        market_rate = _market_rate_text(mid, source, target.currency)

    examples = _examples(fair, target, source)
    simulated = payout_name(provider) == "standin"
    line = _line(pick, examples, markup, simulated)
    return Comparison((pick, *examples), pick, line, quote.id, markup, market_rate)


def _market_rate_text(mid: Decimal, source: str, target_currency: str) -> str:
    """`mid` is target units per source unit; people think in what one unit of the target costs."""
    price = Decimal(1) / mid
    places = Decimal("0.01") if price >= 1 else Decimal("0.0001")
    return f"1 {target_currency} = {price.quantize(places, ROUND_HALF_UP):,} {source}"


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


def _line(pick: Option, examples: list[Option], markup: Decimal | None, simulated: bool) -> str:
    """One plain sentence for the agent to repeat word for word: the price, the arrival and the saving on a wire."""
    # The price is written exactly as the PIN page writes it, so the two never disagree.
    line = f"you pay {pick.you_pay} and {_exact(pick.they_receive)} arrives in about a day"
    lead = "Way to pay"
    wire = next((o for o in examples if o.name.startswith(_BANK_WIRE)), None)
    # Without the market rate the examples are priced from the provider's own cost, so they always come out dearer.
    # Any saving claimed against them would be a claim against our own rate.
    if markup is not None and wire is not None:
        saving = wire.you_pay.decimal - pick.you_pay.decimal
        gap = _gap(saving, pick.you_pay.currency)
        if gap is not None:
            # The wire is an illustration, not a quote, and the line says so.
            line += f", {gap} {'less' if saving >= 0 else 'more'} than a typical bank wire (example figures)"
            # Only a win over every example counts: matching one is not the cheapest.
            if all(pick.you_pay < o.you_pay for o in examples):
                lead = "Cheapest way"
    line = f"{lead}: {line}."
    return line + " (Test payment, simulated, no real money.)" if simulated else line


def _exact(money: Money) -> str:
    """Whole units when there are no cents, since the amount that arrives must not be rounded."""
    units = money.decimal.to_integral_value()
    return f"{money.currency} {units:,}" if units == money.decimal else str(money)


def _gap(saving: Decimal, currency: str) -> str | None:
    """Whole units, rounded so the gap never flatters: a saving down, an extra cost up. None under one unit."""
    units = saving.to_integral_value(ROUND_FLOOR) if saving >= 0 else (-saving).to_integral_value(ROUND_CEILING)
    return f"{currency} {units:,}" if units else None
