"""The market rate (cached, and silent when it can't be had) and the comparison of ways to send.

Every test that reaches karo.rates uses the `exchange_api` mock: nothing here touches the network.
Every test names the bug it would catch.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from conftest import EXCHANGE_URL, rates_payload
from karo import audit, comparison, db, rates, store
from karo.money import Money
from karo.providers.flutterwave import FlutterwaveProvider
from karo.providers.hybrid import HybridProvider
from karo.providers.standin import StandInProvider

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam_otieno"
OTHER = "fam_mwangi"
KES_URL = EXCHANGE_URL.format(source="KES")
GBP_6000 = Money.parse("6000.00", "GBP")


def policy(currency: str) -> dict:
    return {
        "payment_cap": {"minor": 260000000, "currency": currency},
        "allowance_monthly_cap": {"minor": 17300000, "currency": currency},
        "daily_cap": {"minor": 346000000, "currency": currency},
        "new_payee_threshold": {"minor": 3460000, "currency": currency},
        "new_payee_wait_seconds": 86400,
        "quote_ttl_seconds": 900,
        "pin_max_attempts": 3,
        "pin_lockout_seconds": 900,
    }


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init(connection)
    for family_id, country, currency in ((FAM, "KE", "KES"), (OTHER, "UG", "UGX")):
        connection.execute(
            "INSERT INTO families (id, name, home_country, home_currency, timezone, policy_json) "
            "VALUES (?, 'Test family', ?, ?, 'Africa/Nairobi', ?)",
            (family_id, country, currency, json.dumps(policy(currency))),
        )
    yield connection
    connection.close()


def provider_at(rate: str, *, name: str = "standin") -> StandInProvider:
    """A provider that sells GBP at `rate` GBP per KES, and says its name is `name`."""
    provider = StandInProvider(
        rates={("KES", "GBP"): Decimal(rate), ("UGX", "GBP"): Decimal("0.0002")}, secret="s", base_url="https://k.test"
    )
    provider.name = name
    return provider


# --- the market rate ---------------------------------------------------------------------------


def test_the_market_rate_is_target_units_per_one_source_unit(conn, exchange_api):
    # Bug caught: the rate read for the wrong currency, or the wrong way round.
    exchange_api.get(KES_URL).respond(json=rates_payload(GBP=0.0058, USD=0.0077))
    assert rates.mid_market(conn, "KES", "GBP", NOW) == Decimal("0.0058")


def test_the_request_is_made_with_a_five_second_timeout(conn, exchange_api):
    # Bug caught: no timeout, so a slow rate service freezes the chat.
    route = exchange_api.get(KES_URL).respond(json=rates_payload(GBP=0.0058))
    rates.mid_market(conn, "KES", "GBP", NOW)
    assert route.calls.last.request.extensions["timeout"]["read"] == 5.0


def test_a_rate_under_twelve_hours_old_is_not_fetched_again(conn, exchange_api):
    # Bug caught: hitting the free rate service on every quote, which gets the app rate-limited.
    route = exchange_api.get(KES_URL).respond(json=rates_payload(GBP=0.0058))
    rates.mid_market(conn, "KES", "GBP", NOW)
    exchange_api.get(KES_URL).respond(json=rates_payload(GBP=0.0099))
    assert rates.mid_market(conn, "KES", "GBP", NOW + timedelta(hours=11, minutes=59)) == Decimal("0.0058")
    assert route.call_count == 1


def test_a_rate_twelve_hours_old_is_fetched_again(conn, exchange_api):
    # Bug caught: a cache with no expiry, so the rate shown drifts further from the market every day.
    exchange_api.get(KES_URL).respond(json=rates_payload(GBP=0.0058))
    rates.mid_market(conn, "KES", "GBP", NOW)
    exchange_api.get(KES_URL).respond(json=rates_payload(GBP=0.0061))
    assert rates.mid_market(conn, "KES", "GBP", NOW + timedelta(hours=12)) == Decimal("0.0061")


def test_each_source_currency_has_its_own_cache_entry(conn, exchange_api):
    # Bug caught: one family's cached rates being served for a family paying from another currency.
    exchange_api.get(KES_URL).respond(json=rates_payload(GBP=0.0058))
    exchange_api.get(EXCHANGE_URL.format(source="UGX")).respond(json=rates_payload(GBP=0.0002))
    rates.mid_market(conn, "KES", "GBP", NOW)
    assert rates.mid_market(conn, "UGX", "GBP", NOW) == Decimal("0.0002")
    keys = {r["key"] for r in conn.execute("SELECT key FROM settings WHERE key LIKE 'rates:%'")}
    assert keys == {"rates:KES", "rates:UGX"}


OFFLINE = [
    pytest.param(httpx.ConnectError("no network"), id="connection error"),
    pytest.param(httpx.ReadTimeout("too slow"), id="timeout"),
    pytest.param(httpx.Response(503), id="server error"),
]

MALFORMED = [
    pytest.param(httpx.Response(200, content=b"<html>not json</html>"), id="not json"),
    pytest.param(httpx.Response(200, json=[1, 2]), id="not an object"),
    pytest.param(httpx.Response(200, json={"result": "error", "error-type": "unsupported-code"}), id="result error"),
    pytest.param(httpx.Response(200, json=rates_payload(USD=0.0077)), id="currency missing"),
    pytest.param(httpx.Response(200, json=rates_payload(GBP=0)), id="rate of zero"),
    pytest.param(httpx.Response(200, json=rates_payload(GBP="soon")), id="rate not a number"),
]


@pytest.mark.parametrize("reply", OFFLINE + MALFORMED)
def test_with_no_cache_a_failed_lookup_gives_none(conn, exchange_api, reply):
    # Bug caught: an outage or a bad reply crashing the quote, instead of just leaving the markup out.
    exchange_api.get(KES_URL).mock(side_effect=[reply])
    assert rates.mid_market(conn, "KES", "GBP", NOW) is None


@pytest.mark.parametrize("reply", OFFLINE + MALFORMED)
def test_a_failed_lookup_gives_the_cached_rate_even_when_it_is_old(conn, exchange_api, reply):
    # Bug caught: throwing away a usable rate, or overwriting it with a bad reply, because it is past twelve hours.
    exchange_api.get(KES_URL).respond(json=rates_payload(GBP=0.0058))
    rates.mid_market(conn, "KES", "GBP", NOW)
    exchange_api.get(KES_URL).mock(side_effect=[reply])
    assert rates.mid_market(conn, "KES", "GBP", NOW + timedelta(days=3)) == Decimal("0.0058")


def test_an_error_that_is_not_about_the_rate_service_is_not_swallowed(conn, exchange_api):
    # Bug caught: a bare except hiding a real bug in our own code as "offline".
    exchange_api.get(KES_URL).mock(side_effect=[RuntimeError("bug")])
    with pytest.raises(RuntimeError):
        rates.mid_market(conn, "KES", "GBP", NOW)


# --- comparing the ways to send ----------------------------------------------------------------


def compare(conn, provider, exchange_api, *, mid: float | None = 0.005, family_id: str = FAM, target=GBP_6000):
    """The comparison, with the market rate for KES to GBP at `mid` GBP per KES, or the rate service down."""
    source = "UGX" if family_id == OTHER else "KES"
    route = exchange_api.get(EXCHANGE_URL.format(source=source))
    if mid is None:
        route.mock(side_effect=[httpx.ConnectError("offline")])
    else:
        route.respond(json=rates_payload(GBP=mid))
    return comparison.compare(conn, provider, family_id=family_id, target=target, now=NOW)


def test_the_provider_rate_is_marked_up_over_the_market_by_what_it_costs_extra(conn, exchange_api):
    # Bug caught: the markup measured from the wrong base (the rate instead of the cost), or off by a factor of 100.
    result = compare(conn, provider_at("0.0049"), exchange_api, mid=0.005)
    assert result.markup_pct == Decimal("2.04")  # KES 1,224,489.80 paid against a fair KES 1,200,000.00


def test_a_provider_cheaper_than_the_market_has_a_negative_markup_and_its_line_shows_no_minus(conn, exchange_api):
    # Bug caught: a minus sign in the line a person reads, such as "KES -24,000 less than a bank wire".
    result = compare(conn, provider_at("0.0051"), exchange_api, mid=0.005)
    assert result.markup_pct == Decimal("-1.96")
    assert re.search(r"-\s*\d", result.line) is None


def test_the_pick_is_the_provider_route_and_it_is_the_one_that_was_quoted(conn, exchange_api):
    # Bug caught: recommending a route Karo can't execute, or a pick whose price isn't the saved quote's.
    result = compare(conn, provider_at("0.0049"), exchange_api)
    quote = store.get_quote(conn, FAM, result.quote_id)
    assert result.pick is result.options[0]
    assert result.pick.is_example is False and result.pick.speed == "about 1 day"
    assert (quote.source, quote.target) == (Money.parse("1224489.80", "KES"), GBP_6000)
    assert (result.pick.you_pay, result.pick.they_receive) == (quote.source, quote.target)


def test_the_provider_stays_the_pick_when_an_example_is_cheaper(conn, exchange_api):
    # Bug caught: the pick switching to a cheaper example that Karo can't actually send through.
    result = compare(conn, provider_at("0.0040"), exchange_api, mid=0.005)  # KES 1.5m, against a wire at KES 1.25m
    assert result.pick.is_example is False
    assert result.pick.you_pay == Money.parse("1500000.00", "KES")


def test_the_examples_are_labelled_as_examples_and_priced_from_the_market_rate(conn, exchange_api):
    # Bug caught: made-up figures presented as live quotes, or the fixed fee left out of the price.
    result = compare(conn, provider_at("0.0049"), exchange_api, mid=0.005)
    examples = result.options[1:]
    assert [o.name for o in examples] == ["Bank wire (example)", "Money transfer shop (example)"]
    assert all(o.is_example for o in examples)
    assert [o.speed for o in examples] == ["3-5 days", "1-2 days"]
    # 6000 / 0.005 = 1,200,000 at the market rate; plus 4% and KES 3,500, or plus 3% and KES 1,500.
    assert [o.you_pay for o in examples] == [Money.parse("1251500.00", "KES"), Money.parse("1237500.00", "KES")]
    assert all(o.they_receive == GBP_6000 for o in examples)


def test_example_prices_are_rounded_up_to_the_minor_unit(conn, exchange_api):
    # Bug caught: rounding to nearest, which can show a price a cent under what it would really cost.
    result = compare(conn, provider_at("0.0049"), exchange_api, mid=0.0053)
    assert result.options[1].you_pay == Money.parse("1180858.50", "KES")  # 1,180,858.4905...


def test_without_the_market_rate_the_examples_are_priced_from_the_provider_rate(conn, exchange_api):
    # Bug caught: the examples vanishing, or crashing, when the rate service is down.
    result = compare(conn, provider_at("0.0049"), exchange_api, mid=None)
    assert result.markup_pct is None
    # KES 1,224,489.80 + 4% + KES 3,500, and + 3% + KES 1,500, each rounded up.
    assert [o.you_pay for o in result.options[1:]] == [
        Money.parse("1276969.40", "KES"),
        Money.parse("1262724.50", "KES"),
    ]


def test_the_line_names_the_side_that_quoted_when_collections_and_payouts_differ(conn, exchange_api):
    # Bug caught: a simulated stand-in quote labelled "Flutterwave" because the hybrid is named for its collections.
    hybrid = HybridProvider(
        collections=FlutterwaveProvider("FLWSECK_TEST-made-up-key-0000", "made-up-webhook-hash-0000"),
        payouts=provider_at("0.0049"),
    )
    result = compare(conn, hybrid, exchange_api, mid=0.005)
    assert result.options[0].name == "Test payment (simulated, no real money)"


def test_the_line_is_one_plain_sentence_with_the_price_the_arrival_and_the_saving(conn, exchange_api):
    # Bug caught: a line that doesn't match the figures, or still carries the jargon the family found too technical.
    result = compare(conn, provider_at("0.0049", name="flutterwave"), exchange_api, mid=0.005)
    assert result.line == (
        "Cheapest way: you pay KES 1,224,490 and GBP 6,000 arrives in about a day, KES 27,010 less than a bank wire."
    )


def test_the_line_has_no_market_rate_clause_and_so_no_attribution(conn, exchange_api):
    # Bug caught: the market-rate sentence, or its attribution, left in a line that no longer makes the claim.
    result = compare(conn, provider_at("0.0049", name="flutterwave"), exchange_api, mid=0.005)
    assert "market rate" not in result.line and "Exchange Rate API" not in result.line
    assert "example figures" not in result.line and "typical" not in result.line


def test_the_price_is_rounded_up_and_the_saving_down_so_neither_flatters(conn, exchange_api):
    # Bug caught: rounding to nearest, which shows a price a unit under what it is, or a saving a unit over.
    result = compare(conn, provider_at("0.00577"), exchange_api, mid=0.005)  # pays KES 1,039,861.36; wire 1,251,500
    assert "you pay KES 1,039,862 and" in result.line  # up from .36
    assert "KES 211,638 less than a bank wire" in result.line  # down from 211,638.64


def test_what_arrives_keeps_its_cents_when_it_has_any(conn, exchange_api):
    # Bug caught: "GBP 6,000 arrives" for a fee of GBP 6,000.50.
    result = compare(conn, provider_at("0.0049"), exchange_api, target=Money.parse("6000.50", "GBP"))
    assert "and GBP 6,000.50 arrives in about a day" in result.line


def test_the_stand_in_provider_is_labelled_as_a_test_payment_with_no_real_money(conn, exchange_api):
    # Bug caught: a demo run looking like a real provider quote.
    result = compare(conn, provider_at("0.0049", name="standin"), exchange_api)
    assert result.line.startswith("Cheapest way: you pay KES 1,224,490 ")
    assert result.line.endswith(" (Test payment, simulated, no real money.)")


def test_a_real_provider_line_carries_no_test_payment_note(conn, exchange_api):
    # Bug caught: telling a family that a real payment is simulated.
    result = compare(conn, provider_at("0.0049", name="flutterwave"), exchange_api)
    assert "Test payment" not in result.line and "simulated" not in result.line


def test_a_provider_dearer_than_the_bank_wire_says_how_much_more_and_is_not_called_cheapest(conn, exchange_api):
    # Bug caught: "KES -248,500 less than a bank wire", or the dearer route called the cheapest way.
    result = compare(conn, provider_at("0.0040"), exchange_api, mid=0.005)
    assert "KES 248,500 more than a bank wire" in result.line
    assert "less than" not in result.line and "Cheapest" not in result.line
    assert re.search(r"-\s*\d", result.line) is None


def test_the_route_is_not_called_the_cheapest_when_a_shop_example_is_cheaper(conn, exchange_api):
    # Bug caught: claiming "Cheapest way" because it beats the bank wire, when the money shop example is cheaper.
    # Pays KES 1,250,000, against a wire at 1,251,500 and a shop at 1,237,500.
    result = compare(conn, provider_at("0.0048"), exchange_api, mid=0.005)
    assert result.line.startswith("Way to pay: you pay KES 1,250,000 ")
    assert "KES 1,500 less than a bank wire" in result.line


def test_without_the_market_rate_the_line_makes_no_comparison_at_all(conn, exchange_api):
    # Bug caught: a saving stated against our own rate (the examples are priced from it, so it is always "cheaper"),
    # a claim of being the cheapest, or a mention of a market rate that wasn't used.
    result = compare(conn, provider_at("0.0049"), exchange_api, mid=None)
    assert result.line == (
        "Way to pay: you pay KES 1,224,490 and GBP 6,000 arrives in about a day. "
        "(Test payment, simulated, no real money.)"
    )
    assert [o.is_example for o in result.options] == [False, True, True]  # the options are still there, as examples


def test_a_family_not_paid_in_kes_gets_no_example_it_cannot_price(conn, exchange_api):
    # Bug caught: the KES fixed fee added to another currency, raising CurrencyMismatch out of the comparison.
    result = compare(conn, provider_at("0.0049"), exchange_api, family_id=OTHER)
    assert [o.is_example for o in result.options] == [False]
    assert "bank wire" not in result.line and "Cheapest" not in result.line


def test_one_quote_is_saved_for_the_family_and_no_other_family_can_see_it(conn, exchange_api):
    # Bug caught: a quote that can't be proposed against, saved once per option, or readable by another family.
    result = compare(conn, provider_at("0.0049"), exchange_api)
    assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
    saved = store.get_quote(conn, FAM, result.quote_id)
    assert saved is not None and saved.family_id == FAM
    assert store.get_quote(conn, OTHER, result.quote_id) is None
    assert [e["event"] for e in audit.events(conn, FAM)] == ["quote.created"]


def test_a_pair_the_provider_cannot_price_raises_and_saves_nothing(conn, exchange_api):
    # Bug caught: a comparison built on no quote, or a stray quote saved for an amount nobody can pay.
    with pytest.raises(ValueError, match="No stand-in rate"):
        compare(conn, provider_at("0.0049"), exchange_api, target=Money.parse("100", "JPY"))
    assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
