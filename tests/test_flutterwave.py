"""The Flutterwave adapter and the hybrid provider, with no network. Every test names the bug it would catch.

The response bodies are shaped like the real test-mode answers saved in proof/smoke/. Values that identify a person,
a key or a hash are made up. The rate and transfer shapes follow Flutterwave's v3 docs: the smoke runs could not reach
`/transfers/rates`, and this merchant is not allowed to make transfers.
"""

import json
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from karo import approvals, audit, collections, db, executor, ledger, notices, proposals, quotes, seed, store
from karo.config import Settings, load_settings
from karo.domain import Kind, Payee
from karo.money import Money
from karo.providers.base import Notice, Provider, payout_name
from karo.providers.flutterwave import BASE_URL, FlutterwaveProvider
from karo.providers.hybrid import HybridProvider, flutterwave_hybrid
from karo.providers.standin import StandInProvider

KEY = "FLWSECK_TEST-made-up-key-0000"
HASH = "made-up-webhook-hash-0000"
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)

PAYER = {
    "payer_name": "Baraka Otieno",
    "payer_email": "baraka@example.test",
    "payer_phone": "+254700000001",
    "redirect_url": "https://karo.example/paid",
}
KENYAN_PHONE = Payee.of("Jabali Otieno", country="KE", currency="KES", phone="254700000002")
UK_UNIVERSITY = Payee.of(
    "University of Westbridge", country="GB", currency="GBP", sort_code="20-40-60", account_number="41234567"
)


def kes(amount: str) -> Money:
    return Money.parse(amount, "KES")


def gbp(amount: str) -> Money:
    return Money.parse(amount, "GBP")


def reply(message: str, data) -> dict:
    return {"status": "success", "message": message, "data": data}


def refusal(message: str) -> dict:
    return {"status": "error", "message": message, "data": None}


# The shape of the verified M-Pesa charge in proof/smoke (step 4b), with the identifying values replaced.
VERIFIED = reply(
    "Transaction fetched successfully",
    {
        "id": 10562376,
        "tx_ref": "karo-col_1",
        "flw_ref": "2325352590",
        "amount": 100,
        "currency": "KES",
        "charged_amount": 100,
        "processor_response": "Transaction Successful",
        "status": "successful",
        "payment_type": "mpesa",
        "created_at": "2026-10-20T00:20:35.000Z",
        "amount_settled": 95.1,
        "customer": {"id": 1, "name": "Test Payer", "phone_number": "254700000001", "email": "payer@example.test"},
    },
)
NOT_FOUND = refusal("No transaction was found for this id")
HOSTED_LINK = reply("Hosted Link", {"link": "https://checkout-v2.dev-flutterwave.com/v3/hosted/pay/made-up-link"})


def transfer(status: str = "NEW", **changes) -> dict:
    data = {
        "id": 190626,
        "account_number": "254700000002",
        "full_name": "Jabali Otieno",
        "currency": "KES",
        "debit_currency": "KES",
        "amount": 5000,
        "status": status,
        "reference": "karo-prop_1_PMCK",
        "narration": "Monthly allowance",
    }
    return reply("Transfer Queued Successfully", data | changes)


def charge_notice(tx_ref: str = "karo-col_1", event_id: int = 10562376) -> bytes:
    body = {"event": "charge.completed", "data": {"id": event_id, "tx_ref": tx_ref, "status": "successful"}}
    return json.dumps(body).encode()


@pytest.fixture
def flw():
    """The Flutterwave API, mocked. Any request that is not given a route fails the test."""
    with respx.mock(base_url=BASE_URL, assert_all_mocked=True, assert_all_called=False) as router:
        yield router


@pytest.fixture
def provider() -> FlutterwaveProvider:
    return FlutterwaveProvider(KEY, HASH)


def sent(route) -> httpx.Request:
    return route.calls.last.request


def sent_json(route) -> dict:
    return json.loads(sent(route).content)


# --- keys -------------------------------------------------------------------------------------


def test_a_live_key_is_refused_when_the_provider_is_built():
    # Bug caught: the adapter built with a live key, and a demo run moving real money.
    with pytest.raises(RuntimeError, match="test key"):
        FlutterwaveProvider("FLWSECK-live-made-up-0000", HASH)


def test_an_empty_webhook_hash_is_refused():
    # Bug caught: an empty hash matching an empty verif-hash header, so anyone could post a notice.
    with pytest.raises(RuntimeError, match="FLW_WEBHOOK_HASH"):
        FlutterwaveProvider(KEY, "")


def test_every_request_carries_the_key_as_a_bearer_token(provider, flw):
    # Bug caught: a request sent with no or the wrong Authorization header, so every call is refused.
    route = flw.get("/transactions/verify_by_reference").respond(200, json=VERIFIED)
    provider.verify_collection("karo-col_1")
    assert sent(route).headers["authorization"] == f"Bearer {KEY}"


def test_the_requests_have_a_timeout(provider):
    # Bug caught: a stalled Flutterwave hanging a worker thread, and with it the page that asked.
    assert provider._client.timeout.read is not None and provider._client.timeout.read <= 30


def test_the_adapter_satisfies_the_provider_interface(provider):
    # Bug caught: a method renamed or missing here, so the web app breaks when it swaps this in.
    assert isinstance(provider, Provider)
    assert provider.name == "flutterwave"


# --- quote ------------------------------------------------------------------------------------


def test_quote_asks_for_the_target_amount_and_rounds_the_source_up(provider, flw):
    # Bug caught: rounding the family's cost down, so the university receives less than the invoice.
    route = flw.get("/transfers/rates").respond(
        200,
        json=reply(
            "Transfer amount fetched successfully",
            {
                "rate": 173.0,
                "source": {"currency": "KES", "amount": 1038062.281},
                "destination": {"currency": "GBP", "amount": 6000},
            },
        ),
    )
    quote = provider.quote("KES", gbp("6000.00"))
    assert parse_qs(sent(route).url.query.decode()) == {
        "amount": ["6000.00"],
        "source_currency": ["KES"],
        "destination_currency": ["GBP"],
    }
    assert quote.source == Money(103806229, "KES")
    assert (quote.target, quote.provider) == (gbp("6000.00"), "flutterwave")
    assert quote.rate.quantize(Decimal("0.00001")) == Decimal("0.00578")


def test_quote_in_the_same_currency_makes_no_request(provider, flw):
    # Bug caught: a paid-in-GBP fee being sent to the rates endpoint, which has no such pair.
    quote = provider.quote("GBP", gbp("6000.00"))
    assert (quote.source, quote.target, quote.rate) == (gbp("6000.00"), gbp("6000.00"), Decimal(1))
    assert not flw.calls


def test_quote_refuses_a_reply_in_other_currencies(provider, flw):
    # Bug caught: pricing GBP 6,000 with a rate for some other pair, so the family pays the wrong amount.
    flw.get("/transfers/rates").respond(
        200,
        json=reply(
            "ok",
            {
                "rate": 1,
                "source": {"currency": "UGX", "amount": 100},
                "destination": {"currency": "GBP", "amount": 6000},
            },
        ),
    )
    with pytest.raises(ValueError, match="KES to GBP"):
        provider.quote("KES", gbp("6000.00"))


def test_quote_refuses_a_reply_that_prices_the_payment_at_nothing(provider, flw):
    # Bug caught: a zero cost from a faulty reply becoming a free quote (or a crash dividing by it).
    flw.get("/transfers/rates").respond(
        200,
        json=reply(
            "ok",
            {
                "rate": 0,
                "source": {"currency": "KES", "amount": 0},
                "destination": {"currency": "GBP", "amount": 6000},
            },
        ),
    )
    with pytest.raises(ValueError, match="KES to GBP"):
        provider.quote("KES", gbp("6000.00"))


def test_quote_with_no_rate_raises_the_error_the_stand_in_raises(provider, flw):
    # Bug caught: the agent tool, which only catches ValueError for "no price", crashing on a refused rate.
    flw.get("/transfers/rates").respond(400, json=refusal("Currency not supported"))
    with pytest.raises(ValueError, match="KES to GBP"):
        provider.quote("KES", gbp("6000.00"))


# --- create_collection ------------------------------------------------------------------------


def test_create_collection_asks_for_a_hosted_link_in_the_countrys_payment_option(provider, flw):
    # Bug caught: the payer sent to a checkout without their mobile-money option, or charged the wrong amount.
    route = flw.post("/payments").respond(200, json=HOSTED_LINK)
    created = provider.create_collection(tx_ref="karo-col_1", amount=kes("1500.50"), method="mpesa", **PAYER)
    assert created.link == HOSTED_LINK["data"]["link"]
    assert (created.tx_ref, created.provider) == ("karo-col_1", "flutterwave")
    assert sent_json(route) == {
        "tx_ref": "karo-col_1",
        "amount": 1500.5,
        "currency": "KES",
        "redirect_url": "https://karo.example/paid",
        "payment_options": "mpesa",
        "customer": {"email": "baraka@example.test", "name": "Baraka Otieno", "phonenumber": "+254700000001"},
    }


def test_create_collection_maps_the_karo_method_to_flutterwaves_option_name(provider, flw):
    # Bug caught: sending Karo's own method id (mtn_ug), which Flutterwave does not know, so Uganda cannot pay.
    route = flw.post("/payments").respond(200, json=HOSTED_LINK)
    provider.create_collection(tx_ref="karo-col_2", amount=Money(5000, "UGX"), method="mtn_ug", **PAYER)
    body = sent_json(route)
    assert (body["payment_options"], body["amount"], body["currency"]) == ("mobilemoneyuganda", 5000, "UGX")


def test_create_collection_leaves_out_a_phone_number_the_payer_does_not_have(provider, flw):
    # Bug caught: sending "phonenumber": null, which Flutterwave rejects.
    route = flw.post("/payments").respond(200, json=HOSTED_LINK)
    provider.create_collection(
        tx_ref="karo-col_3", amount=kes("100"), method="card", **(PAYER | {"payer_phone": None})
    )
    assert "phonenumber" not in sent_json(route)["customer"]


def test_create_collection_with_an_unknown_method_fails_before_any_request(provider, flw):
    # Bug caught: a typo in a country's settings sending Flutterwave a payment option it did not offer.
    with pytest.raises(ValueError, match="carrier_pigeon"):
        provider.create_collection(tx_ref="karo-col_4", amount=kes("100"), method="carrier_pigeon", **PAYER)
    assert not flw.calls


def test_create_collection_that_flutterwave_refuses_raises_and_gives_no_link(provider, flw):
    # Bug caught: an error body read as a link, so the payer is sent a message with no way to pay.
    flw.post("/payments").respond(400, json=refusal("Invalid currency"))
    with pytest.raises(ValueError, match="Invalid currency"):
        provider.create_collection(tx_ref="karo-col_5", amount=kes("100"), method="mpesa", **PAYER)


# --- verify_collection ------------------------------------------------------------------------


def test_verify_collection_succeeds_on_a_successful_transaction_with_amount_and_currency_from_flutterwave(
    provider, flw
):
    # Bug caught: crediting the amount Karo asked for instead of the amount Flutterwave says arrived.
    route = flw.get("/transactions/verify_by_reference").respond(200, json=VERIFIED)
    status = provider.verify_collection("karo-col_1")
    assert parse_qs(sent(route).url.query.decode()) == {"tx_ref": ["karo-col_1"]}
    assert (status.tx_ref, status.status, status.amount, status.provider_id) == (
        "karo-col_1",
        "succeeded",
        kes("100"),
        "10562376",
    )


def test_verify_collection_reports_the_currency_flutterwave_saw(provider, flw):
    # Bug caught: a payment in the wrong currency (UGX 100 for KES 100) passing as paid.
    wrong = json.loads(json.dumps(VERIFIED))
    wrong["data"]["currency"] = "UGX"
    flw.get("/transactions/verify_by_reference").respond(200, json=wrong)
    assert provider.verify_collection("karo-col_1").amount == Money(100, "UGX")


@pytest.mark.parametrize("status", ["pending", "failed", "new", "cancelled"])
def test_verify_collection_succeeds_only_on_successful(provider, flw, status):
    # Bug caught: any reply that is not an error being read as paid, so an unpaid link credits the share.
    body = json.loads(json.dumps(VERIFIED))
    body["data"]["status"] = status
    flw.get("/transactions/verify_by_reference").respond(200, json=body)
    assert provider.verify_collection("karo-col_1").status != "succeeded"


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_a_payment_attempt_that_has_not_succeeded_leaves_the_hosted_link_live(provider, flw, status):
    # Bug caught: "failed" replacing the link while the old hosted link can still be paid, so that payment is lost.
    body = json.loads(json.dumps(VERIFIED))
    body["data"]["status"] = status
    flw.get("/transactions/verify_by_reference").respond(200, json=body)
    assert provider.verify_collection("karo-col_1").status == "pending"


def test_a_link_nobody_has_paid_yet_is_pending_not_failed(provider, flw):
    # Bug caught: "no transaction found" read as failed, so every reminder throws away a link that is still good.
    flw.get("/transactions/verify_by_reference").respond(400, json=NOT_FOUND)
    status = provider.verify_collection("karo-col_1")
    assert (status.status, status.amount, status.provider_id) == ("pending", None, None)


def test_verify_collection_raises_when_flutterwave_cannot_be_reached(provider, flw):
    # Bug caught: a network error read as "not paid", so a real payment is never credited and nobody is told.
    flw.get("/transactions/verify_by_reference").mock(side_effect=httpx.ConnectError("offline"))
    with pytest.raises(ConnectionError):
        provider.verify_collection("karo-col_1")


@pytest.mark.parametrize("code", [401, 429, 500, 502])
def test_verify_collection_raises_on_a_reply_that_is_not_a_verdict(provider, flw, code):
    # Bug caught: a rejected key or an outage read as "pending", hiding that nothing was checked.
    flw.get("/transactions/verify_by_reference").respond(code, json=refusal("Something went wrong"))
    with pytest.raises(ConnectionError):
        provider.verify_collection("karo-col_1")


# --- payout -----------------------------------------------------------------------------------


def pay_out(provider: FlutterwaveProvider, payee: Payee = KENYAN_PHONE, amount: Money | None = None, **changes):
    return provider.payout(
        **{
            "reference": "karo-prop_1",
            "amount": amount or kes("5000.00"),
            "debit_currency": "KES",
            "payee": payee,
            "narration": "Monthly allowance",
        }
        | changes
    )


def test_a_payout_sends_the_test_mode_success_reference_and_says_it_is_simulated(provider, flw):
    # Bug caught: a test-mode payout left to stay pending forever, or passed off as real money moved.
    route = flw.post("/transfers").respond(200, json=transfer("NEW"))
    result = pay_out(provider)
    assert sent_json(route)["reference"] == "karo-prop_1_PMCK"
    assert result.simulated is True and "simulated" in result.message.lower()
    assert result.reference == "karo-prop_1"  # Karo's own reference: the executor finds its row by it


def test_a_payout_to_a_kenyan_phone_goes_to_mpesa(provider, flw):
    # Bug caught: a phone payee sent as a bank transfer, with no bank code.
    route = flw.post("/transfers").respond(200, json=transfer("NEW"))
    pay_out(provider)
    assert sent_json(route) == {
        "account_bank": "MPS",
        "account_number": "254700000002",
        "amount": 5000,
        "currency": "KES",
        "debit_currency": "KES",
        "beneficiary_name": "Jabali Otieno",
        "narration": "Monthly allowance",
        "reference": "karo-prop_1_PMCK",
    }


def test_a_payout_to_a_uk_bank_account_carries_the_sort_code_in_meta(provider, flw):
    # Bug caught: the sort code dropped, so a GBP transfer cannot be routed to the university.
    route = flw.post("/transfers").respond(200, json=transfer("NEW", currency="GBP"))
    pay_out(provider, UK_UNIVERSITY, gbp("6000.00"), debit_currency="KES", narration="Fees INV-77")
    body = sent_json(route)
    assert (body["account_number"], body["beneficiary_name"], body["currency"], body["debit_currency"]) == (
        "41234567",
        "University of Westbridge",
        "GBP",
        "KES",
    )
    assert body["amount"] == 6000
    assert body["meta"] == [
        {
            "routing_number": "20-40-60",
            "bank_name": "University of Westbridge",
            "account_type": "corporate",
            "beneficiary_merchant_name": "University of Westbridge",
        }
    ]


@pytest.mark.parametrize(
    ("flutterwave_status", "karo_status"), [("NEW", "pending"), ("PENDING", "pending"), ("SUCCESSFUL", "succeeded")]
)
def test_a_payout_reply_maps_to_karos_status_and_keeps_the_transfer_id(provider, flw, flutterwave_status, karo_status):
    # Bug caught: a queued transfer read as paid (or a paid one as queued), so the books and Flutterwave disagree.
    flw.post("/transfers").respond(200, json=transfer(flutterwave_status))
    result = pay_out(provider)
    assert (result.status, result.provider_id) == (karo_status, "190626")


def test_a_payout_flutterwave_refuses_because_the_merchant_cannot_transfer_is_failed(provider, flw):
    # Bug caught: a refusal left pending forever, so the family's money stays held for a payment that never happens.
    flw.post("/transfers").respond(400, json=refusal("merchant is not enabled to make transfers."))
    result = pay_out(provider)
    assert result.status == "failed"
    assert "not enabled to make transfers" in result.message


# Flutterwave's own failures, as the saved smoke runs show them: an HTTP 400 that says nothing about the payment.
SMOKE_NETWORK_FAILURES = [
    "Client network socket disconnected before secure TLS connection was established",
    "Cannot read properties of undefined (reading 'filter')",
]


@pytest.mark.parametrize("message", SMOKE_NETWORK_FAILURES)
def test_a_network_failure_flutterwave_reports_as_a_400_leaves_the_payout_pending(provider, flw, message):
    # Bug caught: a payout that may have gone through read as failed, so the executor refunds money that left.
    flw.post("/transfers").respond(400, json=refusal(message))
    with pytest.raises(ConnectionError):
        pay_out(provider)


def test_a_refusal_that_is_not_on_the_known_list_leaves_the_payout_pending(provider, flw):
    # Bug caught: any unfamiliar 400 refunding the family; an unknown wording holds the money until someone looks.
    flw.post("/transfers").respond(400, json=refusal("Something Flutterwave has never said before"))
    with pytest.raises(ConnectionError):
        pay_out(provider)


def test_a_payout_refused_for_an_unlisted_ip_is_failed(provider, flw):
    # Bug caught: a refusal made before any transfer exists (the first smoke run) left pending for ever.
    flw.post("/transfers").respond(400, json=refusal("Please enable IP Whitelisting to access this service"))
    assert pay_out(provider).status == "failed"


def test_a_duplicate_reference_is_pending_never_failed(provider, flw):
    # Bug caught: the re-send of a payout that already went through read as failed, refunding money that left.
    flw.post("/transfers").respond(400, json=refusal("Transfer with this reference already exists"))
    result = pay_out(provider)
    assert (result.status, result.provider_id) == ("pending", None)


@pytest.mark.parametrize("code", [401, 403, 429, 500, 503])
def test_a_payout_reply_that_is_not_a_verdict_raises_so_the_row_stays_pending(provider, flw, code):
    # Bug caught: an outage or a bad key read as "failed", which refunds a payment that may be on its way.
    flw.post("/transfers").respond(code, json=refusal("Something went wrong"))
    with pytest.raises(ConnectionError):
        pay_out(provider)


def test_a_payout_cut_off_by_the_network_raises_so_the_row_stays_pending(provider, flw):
    # Bug caught: a dropped connection read as failed, refunding a payment Flutterwave may have accepted.
    flw.post("/transfers").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ConnectionError):
        pay_out(provider)


def test_a_payee_this_adapter_cannot_pay_is_failed_without_a_request(provider, flw):
    # Bug caught: an unsupported payee sent half-filled to Flutterwave, or left pending forever.
    result = pay_out(provider, Payee.of("Someone", country="UG", currency="UGX", phone="256700000001"))
    assert result.status == "failed" and "can't pay" in result.message
    assert not flw.calls


# --- payout_status ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flutterwave_status", "karo_status"),
    [("NEW", "pending"), ("PENDING", "pending"), ("SUCCESSFUL", "succeeded"), ("FAILED", "failed")],
)
def test_payout_status_maps_the_transfer_status(provider, flw, flutterwave_status, karo_status):
    # Bug caught: a FAILED transfer left pending (money held for ever) or a PENDING one read as paid.
    route = flw.get("/transfers/190626").respond(
        200, json=reply("Transfer fetched", transfer(flutterwave_status)["data"])
    )
    result = provider.payout_status(reference="karo-prop_1", provider_id="190626")
    assert route.called
    assert (result.reference, result.status, result.provider_id, result.simulated) == (
        "karo-prop_1",
        karo_status,
        "190626",
        True,
    )


@pytest.mark.parametrize(
    ("code", "body"),
    [(404, refusal("Transfer not found")), (400, refusal("No transfer was found for this id"))],
)
def test_payout_status_not_found_is_pending_never_failed(provider, flw, code, body):
    # Bug caught: "not found" read as failed, so a payout still being created is refunded and later paid anyway.
    flw.get("/transfers/190626").respond(code, json=body)
    result = provider.payout_status(reference="karo-prop_1", provider_id="190626")
    assert (result.status, result.provider_id) == ("pending", None)


@pytest.mark.parametrize("message", SMOKE_NETWORK_FAILURES)
def test_payout_status_raises_on_a_network_failure_reported_as_a_400(provider, flw, message):
    # Bug caught: Flutterwave's own failure read as an answer about the transfer, so the executor re-sends or refunds.
    flw.get("/transfers/190626").respond(400, json=refusal(message))
    with pytest.raises(ConnectionError):
        provider.payout_status(reference="karo-prop_1", provider_id="190626")


def test_payout_status_with_no_transfer_id_is_pending_without_a_request(provider, flw):
    # Bug caught: asking Flutterwave for transfers/None, or reading "we never got an id" as a failure.
    result = provider.payout_status(reference="karo-prop_1", provider_id=None)
    assert (result.status, result.provider_id, result.message) == ("pending", None, "not found")
    assert not flw.calls


def test_payout_status_raises_when_flutterwave_cannot_be_reached(provider, flw):
    # Bug caught: a network error read as "failed" or "not found", so the executor refunds or re-sends.
    flw.get("/transfers/190626").mock(side_effect=httpx.ConnectError("offline"))
    with pytest.raises(ConnectionError):
        provider.payout_status(reference="karo-prop_1", provider_id="190626")


# --- parse_notice -----------------------------------------------------------------------------


def test_a_notice_with_the_right_hash_is_accepted(provider):
    # Bug caught: a genuine charge.completed notice refused, so no relative's payment is ever credited.
    notice = provider.parse_notice({"verif-hash": HASH}, charge_notice())
    assert notice == Notice("flutterwave", "10562376", "karo-col_1", None)


def test_a_notice_with_the_wrong_hash_is_refused(provider):
    # Bug caught: anyone who knows the webhook address crediting a share.
    assert provider.parse_notice({"verif-hash": "not-the-hash"}, charge_notice()) is None


def test_a_notice_with_no_hash_is_refused(provider):
    # Bug caught: a missing header treated as "nothing to check".
    assert provider.parse_notice({}, charge_notice()) is None


def test_the_hash_header_is_found_whatever_its_capitals(provider):
    # Bug caught: a genuine notice refused because the web server delivered "Verif-Hash" instead of "verif-hash".
    assert provider.parse_notice({"Verif-Hash": HASH}, charge_notice()) is not None


def test_a_hash_that_is_not_ascii_is_refused_rather_than_crashing(provider):
    # Bug caught: hmac.compare_digest raising on a non-ASCII str, so a forged header turns into a 500.
    assert provider.parse_notice({"verif-hash": "héllo"}, charge_notice()) is None


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"event": "charge.completed"}', b'{"data": {"tx_ref": "karo-col_1"}}', b'{"data": 5}'],
)
def test_a_notice_with_the_right_hash_but_no_event_id_is_refused(provider, body):
    # Bug caught: an unreadable body raising instead of being turned away, or crediting with no id to dedupe on.
    assert provider.parse_notice({"verif-hash": HASH}, body) is None


def test_a_notice_for_something_other_than_a_charge_has_no_tx_ref(provider):
    # Bug caught: a transfer notice's id mistaken for a charge, crediting a share that was never paid.
    body = json.dumps({"event": "transfer.completed", "data": {"id": 7, "reference": "karo-prop_1_PMCK"}}).encode()
    notice = provider.parse_notice({"verif-hash": HASH}, body)
    assert (notice.tx_ref, notice.reference) == (None, "karo-prop_1_PMCK")


# --- the hybrid -------------------------------------------------------------------------------


@pytest.fixture
def standin() -> StandInProvider:
    return StandInProvider.from_config("made-up-standin-secret", "https://karo.example")


def test_the_hybrid_sends_collections_to_flutterwave_and_the_rest_to_the_stand_in(provider, standin, flw):
    # Bug caught: a payout or a quote leaking to Flutterwave (or a collection staying on the stand-in) in a mixed setup.
    link = flw.post("/payments").respond(200, json=HOSTED_LINK)
    flw.get("/transactions/verify_by_reference").respond(200, json=VERIFIED)
    hybrid = HybridProvider(collections=provider, payouts=standin)

    created = hybrid.create_collection(tx_ref="karo-col_1", amount=kes("100"), method="mpesa", **PAYER)
    verified = hybrid.verify_collection("karo-col_1")
    notice = hybrid.parse_notice({"verif-hash": HASH}, charge_notice())
    assert (created.provider, verified.status, notice.provider) == ("flutterwave", "succeeded", "flutterwave")
    assert link.called and len(flw.calls) == 2

    quote = hybrid.quote("KES", gbp("6000.00"))
    paid = pay_out(hybrid, UK_UNIVERSITY, gbp("6000.00"))
    status = hybrid.payout_status(reference="karo-prop_1", provider_id=paid.provider_id)
    assert (quote.provider, paid.status, status.status) == ("standin", "succeeded", "succeeded")
    assert len(flw.calls) == 2 and standin.payout_calls == 1  # still only the two collection calls


def test_the_hybrid_can_send_payouts_to_flutterwave_too(provider, standin, flw):
    # Bug caught: KARO_FLW_PAYOUTS=flutterwave still paying out on the stand-in.
    route = flw.post("/transfers").respond(200, json=transfer("NEW"))
    hybrid = HybridProvider(collections=provider, payouts=provider)
    assert pay_out(hybrid).provider_id == "190626" and route.called
    assert hybrid.payout_name == "flutterwave"


def test_the_hybrid_is_named_for_its_collections_and_names_its_payout_side_separately(provider, standin):
    # Bug caught: the webhook route (named for provider.name) answering to the stand-in's name, not Flutterwave's.
    hybrid = HybridProvider(collections=provider, payouts=standin)
    assert isinstance(hybrid, Provider)
    assert (hybrid.name, hybrid.payout_name) == ("flutterwave", "standin")


def test_by_default_only_the_collections_go_to_flutterwave():
    # Bug caught: payouts going to Flutterwave (whose account here cannot transfer) unless someone asked for them.
    hybrid = flutterwave_hybrid(KEY, HASH, payouts="standin", base_url=BASE_HOST)
    assert (hybrid.name, hybrid.payout_name) == ("flutterwave", "standin")


def test_payouts_go_to_flutterwave_when_asked():
    # Bug caught: KARO_FLW_PAYOUTS=flutterwave quietly leaving the payouts simulated.
    hybrid = flutterwave_hybrid(KEY, HASH, payouts="flutterwave", base_url=BASE_HOST)
    assert (hybrid.name, hybrid.payout_name) == ("flutterwave", "flutterwave")


def test_an_unknown_payout_side_is_refused():
    # Bug caught: a typo in KARO_FLW_PAYOUTS silently choosing a side.
    with pytest.raises(RuntimeError, match="KARO_FLW_PAYOUTS"):
        flutterwave_hybrid(KEY, HASH, payouts="flutterwav", base_url=BASE_HOST)


def test_the_hybrid_refuses_a_live_key():
    # Bug caught: the hybrid wiring bypassing the test-key check, so the demo could run on a live account.
    with pytest.raises(RuntimeError, match="test key"):
        flutterwave_hybrid("FLWSECK-live-made-up-0000", HASH, payouts="standin", base_url=BASE_HOST)


def test_the_flutterwave_settings_are_read_and_payouts_default_to_the_stand_in(monkeypatch):
    # Bug caught: payouts going to Flutterwave (which this merchant cannot do) unless someone asked for it.
    for name in ("FLW_SECRET_KEY", "FLW_WEBHOOK_HASH", "KARO_FLW_PAYOUTS"):
        monkeypatch.delenv(name, raising=False)
    required = {"KARO_PIN_PEPPER": "p-xyz", "KARO_SESSION_SECRET": "s-xyz", "KARO_DEMO_PASSWORD": "d-xyz"}
    for name, value in (required | {"KARO_PROVIDER": "flutterwave"}).items():
        monkeypatch.setenv(name, value)
    assert load_settings(Path("no-such-file.env")).flw_payouts == "standin"

    monkeypatch.setenv("FLW_SECRET_KEY", KEY)
    monkeypatch.setenv("FLW_WEBHOOK_HASH", HASH)
    monkeypatch.setenv("KARO_FLW_PAYOUTS", "flutterwave")
    settings = load_settings(Path("no-such-file.env"))
    assert (settings.flw_secret_key, settings.flw_webhook_hash, settings.flw_payouts) == (KEY, HASH, "flutterwave")


def test_the_flutterwave_secrets_are_not_in_the_settings_repr():
    # Bug caught: a log line or failed assertion showing the settings object and leaking the Flutterwave key or hash.
    shown = repr(Settings(flw_secret_key=KEY, flw_webhook_hash=HASH))
    assert KEY not in shown and HASH not in shown


# --- the provider named in each database row ----------------------------------------------------

PIN = "4821"
PEPPER = "test-pepper"
BASE_HOST = "https://karo.example"


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    # Seeding hashes two PINs with Argon2, which is deliberately slow: do it once and copy the file per test.
    path = tmp_path_factory.mktemp("template") / "karo.db"
    connection = db.connect(path)
    db.init(connection)
    seed.seed_demo(connection, pin=PIN, pepper=PEPPER, now=NOW)
    connection.close()
    return path


@pytest.fixture
def conn(seeded_db, tmp_path):
    target = tmp_path / "karo.db"
    shutil.copyfile(seeded_db, target)
    connection = db.connect(target)
    yield connection
    connection.close()


def column(conn, sql: str) -> list[str]:
    return [row[0] for row in conn.execute(sql)]


def test_each_row_names_the_provider_that_really_handled_it(conn, provider, standin, flw):
    # Bug caught: a simulated payout recorded as Flutterwave's, so the proof export calls fake money real.
    hybrid = HybridProvider(collections=provider, payouts=standin)
    flw.post("/payments").respond(200, json=HOSTED_LINK)

    # money in: a share of the fee is asked for through Flutterwave
    trusted = store.load_state(conn, "fam_otieno", NOW).trusted_payees
    westbridge = next(t.payee for t in trusted if t.id == "tp_westbridge")
    fee_id = store.insert_fee(
        conn,
        family_id="fam_otieno",
        payee=westbridge,
        amount=gbp("6000.00"),
        due_date=None,
        invoice_ref="INV-77",
        now=NOW,
    )
    share_id = db.new_id("sh")
    conn.execute(
        "INSERT INTO shares (id, family_id, fee_id, member_id, amount_minor, currency, paid_minor, created_at) "
        "VALUES (?, 'fam_otieno', ?, 'm_baraka', 100000, 'GBP', 0, '2026-10-19T09:00:00+00:00')", (share_id, fee_id)
    )
    collection_id = collections.request_share(
        conn, hybrid, family_id="fam_otieno", share_id=share_id, now=NOW, base_url=BASE_HOST
    )
    assert collection_id is not None
    assert column(conn, "SELECT provider FROM collections") == ["flutterwave"]

    # money out: the fee is paid, quoted and sent by the stand-in
    quote = quotes.create_quote(
        conn, hybrid, family_id="fam_otieno", source_currency="KES", target=gbp("6000.00"), now=NOW
    )
    funds = Money.parse("5000000", "KES") - seed.OPENING_BALANCE
    ledger.post(conn, family_id="fam_otieno", at=NOW, amount=funds, kind="collection", ref="test-funds")
    outcome = proposals.propose(
        conn,
        family_id="fam_otieno",
        actor=store.member(conn, "fam_otieno", "m_amina"),
        kind=Kind.PAY_FEE,
        now=NOW,
        base_url=BASE_HOST,
        amount=quote.target,
        quote_id=quote.id,
        payee=westbridge,
        fee_id=fee_id,
    )
    assert outcome.status == "awaiting_pin"
    row = conn.execute("SELECT text FROM outbox WHERE member_id = 'm_amina' AND text LIKE '%/confirm/%'").fetchone()
    token = row["text"].split("/confirm/")[1].split()[0]
    proposal_id = approvals.confirm(conn, token=token, pin=PIN, pepper=PEPPER, now=NOW).proposal_id
    assert executor.execute(
        conn, hybrid, family_id="fam_otieno", proposal_id=proposal_id, now=NOW, base_url=BASE_HOST
    ).status == "executed"

    assert column(conn, "SELECT provider FROM payouts") == ["standin"]
    assert column(conn, "SELECT provider FROM quotes") == ["standin"]
    assert column(conn, "SELECT provider FROM collections") == ["flutterwave"]


def test_a_flutterwave_notice_credits_the_share_through_the_hybrid(conn, provider, standin, flw):
    # Bug caught: the hybrid dropping the verif-hash header, or the re-check going to the stand-in.
    hybrid = HybridProvider(collections=provider, payouts=standin)
    flw.post("/payments").respond(200, json=HOSTED_LINK)
    share_id = db.new_id("sh")
    conn.execute(
        "INSERT INTO shares (id, family_id, fee_id, member_id, amount_minor, currency, paid_minor, created_at) "
        "VALUES (?, 'fam_otieno', NULL, 'm_baraka', 10000, 'KES', 0, '2026-10-19T09:00:00+00:00')", (share_id,)
    )
    collection_id = collections.request_share(
        conn, hybrid, family_id="fam_otieno", share_id=share_id, now=NOW, base_url=BASE_HOST
    )
    tx_ref = conn.execute("SELECT tx_ref FROM collections WHERE id = ?", (collection_id,)).fetchone()["tx_ref"]
    paid = json.loads(json.dumps(VERIFIED))
    paid["data"].update(tx_ref=tx_ref, amount=100)
    flw.get("/transactions/verify_by_reference").respond(200, json=paid)

    result = notices.handle_notice(
        conn, hybrid, headers={"verif-hash": HASH}, body=charge_notice(tx_ref), now=NOW
    )

    assert result == "credited"
    assert conn.execute("SELECT paid_minor FROM shares WHERE id = ?", (share_id,)).fetchone()[0] == 10000
    assert column(conn, "SELECT provider FROM provider_events") == ["flutterwave"]


def test_a_forged_notice_through_the_hybrid_credits_nothing(conn, provider, standin, flw):
    # Bug caught: a notice with the wrong hash reaching the re-check, or being credited on its tx_ref alone.
    hybrid = HybridProvider(collections=provider, payouts=standin)
    result = notices.handle_notice(
        conn, hybrid, headers={"verif-hash": "forged"}, body=charge_notice(), now=NOW
    )
    assert result == "rejected"
    assert not flw.calls
    assert [e["data"] for e in audit.events(conn, None) if e["event"] == "notice.rejected"] == [
        {"provider": "flutterwave"}
    ]


def test_a_plain_provider_sends_its_payouts_under_its_own_name(standin, provider):
    # Bug caught: the payout name lookup breaking providers that are not hybrids, so every payouts row loses its name.
    assert payout_name(standin) == "standin"
    assert payout_name(provider) == "flutterwave"
    assert payout_name(HybridProvider(collections=provider, payouts=standin)) == "standin"
