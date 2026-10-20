"""The stand-in provider: the whole demo and the tests run on it, with no network and no real money."""

import hashlib
import hmac
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from karo.domain import Payee
from karo.money import Money
from karo.providers.base import Notice, Provider
from karo.providers.standin import StandInProvider

RATES = {
    ("KES", "GBP"): Decimal("0.00578"),
    ("UGX", "GBP"): Decimal("0.000183"),
    ("KES", "UGX"): Decimal("31.6"),
    ("GBP", "KES"): Decimal("173"),
}
SECRET = "standin-test-secret"
BASE_URL = "http://karo.test"
SIGNATURE = "x-standin-signature"


@pytest.fixture
def provider() -> StandInProvider:
    return StandInProvider(rates=RATES, secret=SECRET, base_url=BASE_URL)


def pay_out(provider: StandInProvider, reference: str = "ref-1"):
    return provider.payout(
        reference=reference,
        amount=Money.parse("6000.00", "GBP"),
        debit_currency="KES",
        payee=Payee.of("Test University", account_number="12345678", sort_code="12-34-56"),
        narration="Fees",
    )


def collect(provider: StandInProvider, tx_ref: str = "tx-1", amount: Money | None = None):
    return provider.create_collection(
        tx_ref=tx_ref,
        amount=amount or Money.parse("1000.00", "KES"),
        method="mpesa",
        payer_name="Amina",
        payer_email="amina@example.test",
        payer_phone="+254700000000",
        redirect_url="http://karo.test/done",
    )


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- quote -----------------------------------------------------------------


def test_quote_rounds_the_source_up_so_the_payee_gets_at_least_the_target(provider):
    # Bug caught: rounding the source down (or to nearest), so the university receives less than the invoice.
    target = Money.parse("6000.00", "GBP")
    quote = provider.quote("KES", target)
    assert quote.source == Money(103806229, "KES")
    assert quote.source.decimal * quote.rate >= target.decimal
    one_cent_less = Money(quote.source.minor - 1, "KES")
    assert one_cent_less.decimal * quote.rate < target.decimal


def test_quote_carries_the_target_the_rate_and_the_provider_name(provider):
    # Bug caught: a quote that changes the fixed target or drops the rate the family is later shown.
    quote = provider.quote("KES", Money.parse("6000.00", "GBP"))
    assert (quote.target, quote.rate, quote.provider) == (Money(600000, "GBP"), Decimal("0.00578"), "standin")


def test_quote_from_a_whole_unit_currency_has_no_decimals(provider):
    # Bug caught: treating UGX as a two-decimal currency and quoting 100 times too much or too little.
    quote = provider.quote("UGX", Money.parse("100.00", "GBP"))
    assert quote.source == Money(546449, "UGX")


def test_quote_that_divides_exactly_is_not_rounded_up(provider):
    # Bug caught: always adding one minor unit, so exact amounts cost the family a unit too much.
    quote = provider.quote("KES", Money(3160, "UGX"))
    assert quote.source == Money(10000, "KES")


def test_quote_in_the_same_currency_has_rate_one(provider):
    # Bug caught: a same-currency quote asking for a rate that doesn't exist, or changing the amount.
    quote = provider.quote("GBP", Money.parse("50.00", "GBP"))
    assert (quote.source, quote.rate) == (Money(5000, "GBP"), Decimal(1))


def test_quote_for_a_pair_without_a_rate_is_refused_in_plain_words(provider):
    # Bug caught: an unknown pair coming back as a bare KeyError, or as a made-up rate.
    with pytest.raises(ValueError, match="KES.*USD"):
        provider.quote("KES", Money.parse("10.00", "USD"))


def test_the_shipped_rates_load_from_config():
    # Bug caught: config/standin_rates.json missing, malformed, or its "A->B" keys read the wrong way round.
    loaded = StandInProvider.from_config(SECRET, BASE_URL)
    assert loaded.quote("KES", Money.parse("6000.00", "GBP")).rate == Decimal("0.00578")
    assert loaded.quote("GBP", Money.parse("1000.00", "KES")).rate == Decimal("173")


def test_the_shipped_rates_price_a_send_to_cameroon_from_kenya_and_britain():
    # Bug caught: no stand-in rate into XAF, so a send to Cameroon is refused before it can be priced.
    loaded = StandInProvider.from_config(SECRET, BASE_URL)
    from_kenya = loaded.quote("KES", Money.parse("5000", "XAF"))
    assert (from_kenya.rate, from_kenya.source) == (Decimal("4.34"), Money(115208, "KES"))  # 1152.07.. rounded up
    from_britain = loaded.quote("GBP", Money.parse("7500", "XAF"))
    assert (from_britain.rate, from_britain.source) == (Decimal("750"), Money(1000, "GBP"))


def test_rates_load_from_a_given_path(tmp_path):
    # Bug caught: the path argument ignored, so a test or demo cannot use its own rates.
    path = tmp_path / "rates.json"
    path.write_text(json.dumps({"note": "x", "rates": {"KES->GBP": "0.01"}}), encoding="utf-8")
    loaded = StandInProvider.from_config(SECRET, BASE_URL, path)
    assert loaded.quote("KES", Money.parse("10.00", "GBP")).source == Money(100000, "KES")


# --- collections -----------------------------------------------------------


def test_a_new_collection_has_a_pay_link_and_starts_pending(provider):
    # Bug caught: a collection that looks paid before anyone pays, or a link the web app cannot route.
    created = collect(provider, "tx-9")
    assert (created.tx_ref, created.link, created.provider) == ("tx-9", "http://karo.test/standin/pay/tx-9", "standin")
    status = provider.verify_collection("tx-9")
    assert (status.status, status.amount, status.provider_id) == ("pending", Money.parse("1000.00", "KES"), None)


def test_a_collection_goes_pending_then_succeeded_once_marked_paid(provider):
    # Bug caught: verify still reporting pending after the provider's notice, so money never reaches the share.
    collect(provider)
    assert provider.verify_collection("tx-1").status == "pending"
    provider.mark_paid("tx-1")
    status = provider.verify_collection("tx-1")
    assert (status.status, status.amount, status.provider_id) == ("succeeded", Money.parse("1000.00", "KES"), "SI-tx-1")


def test_an_unknown_tx_ref_fails_verification(provider):
    # Bug caught: a made-up tx_ref being treated as pending or paid.
    status = provider.verify_collection("nope")
    assert (status.tx_ref, status.status, status.amount, status.provider_id) == ("nope", "failed", None, None)


def test_marking_an_unknown_tx_ref_paid_is_an_error(provider):
    # Bug caught: a notice minted for a payment that was never created.
    with pytest.raises(KeyError):
        provider.mark_paid("nope")


def test_creating_a_collection_again_does_not_undo_a_payment(provider):
    # Bug caught: a retried create_collection overwriting a paid collection back to pending.
    collect(provider)
    provider.mark_paid("tx-1")
    collect(provider)
    assert provider.verify_collection("tx-1").status == "succeeded"


# --- notices ---------------------------------------------------------------


def test_a_genuine_notice_parses(provider):
    # Bug caught: the provider's own notice being rejected, so no collection is ever marked paid.
    collect(provider)
    headers, body = provider.mark_paid("tx-1")
    notice = provider.parse_notice(headers, body)
    assert isinstance(notice, Notice)
    assert (notice.provider, notice.tx_ref, notice.reference) == ("standin", "tx-1", None)
    assert notice.event_id == json.loads(body)["event_id"]


def test_marking_paid_twice_repeats_the_same_event_id(provider):
    # Bug caught: a replayed notice getting a fresh event_id, so the duplicate-event check cannot catch it.
    collect(provider)
    first = provider.parse_notice(*provider.mark_paid("tx-1"))
    second = provider.parse_notice(*provider.mark_paid("tx-1"))
    assert first == second


def test_marked_paid_notice_is_signed_with_hmac_sha256_of_the_body(provider):
    # Bug caught: signing something other than the raw body, so the web app's own check can never match.
    collect(provider)
    headers, body = provider.mark_paid("tx-1")
    assert headers == {SIGNATURE: sign(body)}
    assert set(json.loads(body)) == {"event_id", "tx_ref"}


def test_a_notice_with_a_wrong_signature_is_ignored(provider):
    # Bug caught: accepting a forged "payment received" notice.
    collect(provider)
    _, body = provider.mark_paid("tx-1")
    assert provider.parse_notice({SIGNATURE: "0" * 64}, body) is None


def test_a_notice_with_no_signature_is_ignored(provider):
    # Bug caught: treating a missing signature header as fine.
    collect(provider)
    _, body = provider.mark_paid("tx-1")
    assert provider.parse_notice({}, body) is None


def test_a_notice_signed_with_another_secret_is_ignored(provider):
    # Bug caught: a notice from someone who does not hold our secret being believed.
    collect(provider)
    _, body = provider.mark_paid("tx-1")
    assert provider.parse_notice({SIGNATURE: sign(body, "someone-elses-secret")}, body) is None


def test_a_genuine_signature_on_a_changed_body_is_ignored(provider):
    # Bug caught: signature checked but the body then swapped, e.g. for a different tx_ref.
    collect(provider)
    headers, body = provider.mark_paid("tx-1")
    changed = body.replace(b"tx-1", b"tx-2")
    assert provider.parse_notice(headers, changed) is None


def test_a_signed_body_that_is_not_json_is_ignored(provider):
    # Bug caught: bad JSON raising out of the webhook instead of being dropped.
    body = b"not json at all"
    assert provider.parse_notice({SIGNATURE: sign(body)}, body) is None


def test_a_signed_body_with_no_event_id_is_ignored(provider):
    # Bug caught: a notice without an event_id slipping past the duplicate-event check.
    body = json.dumps({"tx_ref": "tx-1"}).encode()
    assert provider.parse_notice({SIGNATURE: sign(body)}, body) is None


def test_a_signature_with_non_ascii_characters_is_ignored_not_an_error(provider):
    # Bug caught: hmac.compare_digest raising TypeError on a str signature that is not ASCII.
    collect(provider)
    _, body = provider.mark_paid("tx-1")
    assert provider.parse_notice({SIGNATURE: "é" * 64}, body) is None


def test_the_signature_header_name_is_found_whatever_its_case(provider):
    # Bug caught: a plain dict with "X-Standin-Signature" being read as unsigned.
    collect(provider)
    headers, body = provider.mark_paid("tx-1")
    assert provider.parse_notice({"X-Standin-Signature": headers[SIGNATURE]}, body) is not None


# --- payouts ---------------------------------------------------------------


def test_a_payout_succeeds_at_once_and_says_it_is_simulated(provider):
    # Bug caught: a stand-in payout that looks like real money moved, or that stays pending forever.
    result = pay_out(provider, "ref-7")
    assert (result.reference, result.provider_id, result.status, result.simulated) == (
        "ref-7",
        "SI-ref-7",
        "succeeded",
        True,
    )
    assert result.message


def test_the_same_reference_twice_makes_one_payout_call(provider):
    # Bug caught: a retried payout paying the university twice.
    first = pay_out(provider)
    second = pay_out(provider)
    assert second == first
    assert provider.payout_calls == 1


def test_different_references_are_separate_payouts(provider):
    # Bug caught: de-duplicating on something other than the reference, so a second real payment is swallowed.
    pay_out(provider, "ref-1")
    pay_out(provider, "ref-2")
    assert provider.payout_calls == 2


def test_payout_status_returns_the_stored_result(provider):
    # Bug caught: a status lookup that invents a result instead of returning what the payout produced.
    made = pay_out(provider, "ref-1")
    assert provider.payout_status(reference="ref-1", provider_id=made.provider_id) == made


def test_payout_status_for_an_unknown_reference_is_pending_not_failed(provider):
    # Bug caught: "not found" read as failed, so a payout still in flight is refunded and later paid unrecorded.
    result = provider.payout_status(reference="nope", provider_id=None)
    assert (result.reference, result.status, result.provider_id) == ("nope", "pending", None)
    assert result.message == "not found"
    assert provider.payout_calls == 0


def test_concurrent_payouts_with_one_reference_count_once(provider):
    # Bug caught: two threads both passing the "already paid?" check and both paying the same reference.
    class SlowLookup(dict):
        """Widens the window between the check and the store, so an unlocked provider is caught every time."""

        def get(self, key, default=None):
            found = super().get(key, default)
            time.sleep(0.02)
            return found

    provider._payouts = SlowLookup()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: pay_out(provider, "ref-race"), range(4)))
    assert provider.payout_calls == 1
    assert len({r.provider_id for r in results}) == 1


# --- shape -----------------------------------------------------------------


def test_the_stand_in_satisfies_the_provider_interface(provider):
    # Bug caught: a method renamed or missing here, so the web app breaks when it swaps in the real provider.
    assert isinstance(provider, Provider)
    assert provider.name == "standin"


def test_the_provider_modules_do_not_pull_in_the_database():
    # Bug caught: a provider importing storage, which ties payments to our database and breaks the adapter swap.
    code = (
        "import sys, karo.providers.base, karo.providers.standin;"
        "bad = [m for m in ('karo.db', 'karo.store') if m in sys.modules];"
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, f"imported: {done.stdout.strip()} {done.stderr}"
