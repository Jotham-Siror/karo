"""The Payaza adapter, with no network. Every test names the bug it would catch.

The request and response bodies follow alianza's src/providers/payaza.ts and the shapes it recorded from Payaza's docs
and test mode. Karo has not yet run against Payaza, so none of these is a real Payaza answer from Karo. Every key,
number and reference here is made up.
"""

import base64
import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest
import respx

from karo.config import Settings, load_settings
from karo.domain import Payee
from karo.money import Money
from karo.providers.base import Notice, Provider, ProviderQuote, payout_name
from karo.providers.hybrid import HybridProvider
from karo.providers.payaza import BASE_URL, PayazaNotEnabled, PayazaProvider, PayazaSender, payaza_hybrid
from karo.providers.standin import StandInProvider
from karo.web.app import build_provider

PUBLIC_KEY = "PZ78-PKTEST-made-up-0000"
SECRET_KEY = "made-up-secret-0000"
SENDER = PayazaSender("Karo Family", "254700000009", "1 Made Up Road")
PAYER = {
    "payer_name": "Baraka Otieno",
    "payer_email": "baraka@example.test",
    "payer_phone": "+254700000001",
    "redirect_url": "https://karo.example/paid",
}
UGANDAN_WALLET = Payee.of("Nakato Supplies", country="UG", currency="UGX", method="mtn_ug", phone="256700000002")
AIRTEL_WALLET = Payee.of("Nakato Supplies", country="UG", currency="UGX", method="airtel_ug", phone="+256750000003")
KENYAN_WALLET = Payee.of("Jabali Otieno", country="KE", currency="KES", method="mpesa", phone="254700000002")

COLLECT = BASE_URL + "/subsidiary/collections/v1/process-collection"
CHECK = BASE_URL + "/subsidiary/collections/v1/check-status"
PAYOUT = BASE_URL + "/payout-receptor/payout"
STATUS = BASE_URL + "/payaza-account/api/v1/mainaccounts/merchant/transaction/"


def kes(amount: str) -> Money:
    return Money.parse(amount, "KES")


def ugx(amount: str) -> Money:
    return Money.parse(amount, "UGX")


# The shapes alianza read from Payaza's mobile-money docs: "09" pending, "00" success, "06" and "96" failed.
PROMPT_SENT = {"response_code": "09", "response_message": "PENDING", "transaction_reference": "karo-col_1"}


def checked(code: str, status: str, **changes) -> dict:
    body = {
        "response_code": code,
        "transaction_reference": "P-C-made-up-1",
        "transaction_amount": "2500.00",
        "transaction_fee": "25.00",
        "transaction_status": status,
        "currency": "KES",
    }
    return body | changes


PAYOUT_ACCEPTED = {
    "response_code": 200,
    "response_message": "Transaction initiated",
    "response_content": {"transaction_status": "09", "response_status": "TRANSACTION_INITIATED", "amount": 50000},
    "resp_code": "09",
}


def payout_state(status: str, **changes) -> dict:
    data = {"transactionReference": "karo-prop_1", "transactionStatus": status, "sessionId": "made-up-session"}
    return {"data": data | changes}


@pytest.fixture
def payaza():
    """The Payaza API, mocked. Any request that is not given a route fails the test."""
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


@pytest.fixture
def provider() -> PayazaProvider:
    return PayazaProvider(
        PUBLIC_KEY, SECRET_KEY, transaction_pin="123456", ugx_account_ref="made-up-ugx-account", sender=SENDER
    )


@pytest.fixture
def unconfigured() -> PayazaProvider:
    return PayazaProvider(PUBLIC_KEY, SECRET_KEY)


def signed(body: bytes, secret: str = SECRET_KEY) -> dict[str, str]:
    return {"x-payaza-signature": base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha512).digest()).decode()}


# --- the provider contract ------------------------------------------------------------------------------------------


def test_payaza_is_a_provider(provider):
    # Bug caught: the adapter missing a method the app calls, found only when it is switched on.
    assert isinstance(provider, Provider)


# --- a collection ---------------------------------------------------------------------------------------------------


def test_a_collection_starts_the_mpesa_prompt_with_the_documented_request(payaza, provider):
    # Bug caught: the wrong auth scheme, a missing product header, a "+" in the number or a wrong bank code, each of
    # which makes Payaza refuse every prompt.
    route = payaza.post(COLLECT).mock(return_value=httpx.Response(200, json=PROMPT_SENT))
    created = provider.create_collection(tx_ref="karo-col_1", amount=kes("2500"), method="mpesa", **PAYER)
    assert (created.tx_ref, created.link, created.provider) == ("karo-col_1", "https://karo.example/paid", "payaza")
    request = route.calls.last.request
    assert request.headers["authorization"] == "Payaza " + base64.b64encode(PUBLIC_KEY.encode()).decode()
    assert (request.headers["x-tenantid"], request.headers["x-productid"]) == ("test", "app")
    sent = json.loads(request.content)
    assert sent["amount"] == 2500
    assert sent["customer_number"] == "254700000001"
    assert (sent["customer_bank_code"], sent["currency_code"], sent["country_code"]) == ("SAFKEN", "KES", "KE")
    assert (sent["transaction_reference"], sent["customer_first_name"], sent["customer_last_name"]) == (
        "karo-col_1",
        "Baraka",
        "Otieno",
    )


def test_a_collection_with_cents_sends_a_json_number(payaza, provider):
    # Bug caught: an amount sent as text or as a rounded whole number, so the payer is prompted for the wrong sum.
    route = payaza.post(COLLECT).mock(return_value=httpx.Response(200, json=PROMPT_SENT))
    provider.create_collection(tx_ref="karo-col_1", amount=kes("2500.55"), method="mpesa", **PAYER)
    assert json.loads(route.calls.last.request.content)["amount"] == 2500.55


def test_a_collection_that_payaza_refuses_raises_and_one_it_never_answered_is_a_connection_error(payaza, provider):
    # Bug caught: a refused prompt handed to the payer as a link that cannot be paid, or an unknown answer read as a
    # refusal.
    request = {"tx_ref": "karo-col_1", "amount": kes("2500"), "method": "mpesa", **PAYER}
    payaza.post(COLLECT).mock(return_value=httpx.Response(200, json={"response_code": "96", "response_message": "No"}))
    with pytest.raises(ValueError, match="would not start"):
        provider.create_collection(**request)
    payaza.post(COLLECT).mock(return_value=httpx.Response(400, json={"message": "Invalid number"}))
    with pytest.raises(ValueError, match="Invalid number"):
        provider.create_collection(**request)
    payaza.post(COLLECT).mock(return_value=httpx.Response(502, json={"message": "bad gateway"}))
    with pytest.raises(ConnectionError):
        provider.create_collection(**request)
    payaza.post(COLLECT).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ConnectionError):
        provider.create_collection(**request)


@pytest.mark.parametrize(
    ("method", "amount"),
    [("card", kes("2500")), ("mtn_ug", ugx("90000")), ("mpesa", Money.parse("10", "GBP"))],
)
def test_a_collection_payaza_is_not_enabled_for_is_refused_before_any_request(payaza, provider, method, amount):
    # Bug caught: inventing an endpoint for a collection alianza never had (card in KES, UGX, GBP).
    with pytest.raises(PayazaNotEnabled, match="not enabled"):
        provider.create_collection(tx_ref="karo-col_1", amount=amount, method=method, **PAYER)
    assert not payaza.calls


def test_a_collection_without_a_phone_number_is_refused_before_any_request(payaza, provider):
    # Bug caught: an M-Pesa prompt sent to an empty number.
    with pytest.raises(ValueError, match="phone"):
        provider.create_collection(
            tx_ref="karo-col_1", amount=kes("2500"), method="mpesa", **PAYER | {"payer_phone": None}
        )
    assert not payaza.calls


# --- checking a collection ------------------------------------------------------------------------------------------


def test_a_completed_collection_is_succeeded_with_its_amount(payaza, provider):
    # Bug caught: a paid collection read as unpaid, or credited at the wrong amount.
    route = payaza.get(CHECK).mock(return_value=httpx.Response(200, json=checked("00", "Completed")))
    status = provider.verify_collection("karo-col_1")
    assert (status.status, status.amount, status.provider_id) == ("succeeded", kes("2500"), "P-C-made-up-1")
    request = route.calls.last.request
    assert dict(request.url.params) == {"transaction_reference": "karo-col_1", "country_code": "KE"}
    assert request.headers["x-productid"] == "app"


def test_a_collection_still_waiting_for_the_payer_is_pending(payaza, provider):
    # Bug caught: crediting the family while the M-Pesa prompt is still open on the payer's phone.
    payaza.get(CHECK).mock(return_value=httpx.Response(200, json=checked("09", "Initialized")))
    status = provider.verify_collection("karo-col_1")
    assert status.status == "pending"
    assert status.provider_id is None


def test_a_collection_payaza_marks_failed_is_failed_so_a_new_link_can_be_made(payaza, provider):
    # Bug caught: a cancelled prompt held as pending forever, so the family can never get a new link.
    payaza.get(CHECK).mock(return_value=httpx.Response(200, json=checked("06", "Failed", transaction_amount="0")))
    assert provider.verify_collection("karo-col_1").status == "failed"


def test_success_with_no_amount_is_refused_not_credited(payaza, provider):
    # Bug caught: crediting a payment whose amount Payaza did not give.
    body = checked("00", "Completed") | {"transaction_amount": None}
    payaza.get(CHECK).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(ValueError, match="without an amount"):
        provider.verify_collection("karo-col_1")


def test_an_unknown_collection_is_pending_not_failed(payaza, provider):
    # Bug caught: a prompt Payaza has not recorded yet read as lost, so Karo makes a second prompt for the same money.
    payaza.get(CHECK).mock(return_value=httpx.Response(404, json={"message": "Transaction not found"}))
    status = provider.verify_collection("karo-col_1")
    assert (status.status, status.amount, status.provider_id) == ("pending", None, None)


@pytest.mark.parametrize("code", [401, 403, 429, 500, 503])
def test_a_collection_check_with_no_usable_answer_raises_instead_of_guessing(payaza, provider, code):
    # Bug caught: a bad key or an outage read as "not paid" or "failed".
    payaza.get(CHECK).mock(
        return_value=httpx.Response(code, json={"message": "Authentication failed", "status": False})
    )
    with pytest.raises(ConnectionError):
        provider.verify_collection("karo-col_1")


def test_an_unreadable_collection_answer_raises(payaza, provider):
    # Bug caught: an HTML error page read as a status.
    payaza.get(CHECK).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ConnectionError, match="can't be read"):
        provider.verify_collection("karo-col_1")


# --- notices --------------------------------------------------------------------------------------------------------


def collection_notice(**changes) -> bytes:
    body = {
        "transaction_reference": "P-C-made-up-1",
        "merchant_reference": "karo-col_1",
        "transaction_status": "Funds Received",
        "status": "Completed",
        "received_from": {"account_name": "Baraka Otieno", "account_number": "254700000001", "bank_name": "M-Pesa"},
        "current_status_date": "2026-10-20T10:00:00",
        "amount_received": "2500.00",
    }
    return json.dumps(body | changes).encode()


def test_a_notice_with_a_good_signature_names_our_reference(provider):
    # Bug caught: a real notice rejected, or filed under Payaza's own P-C- reference instead of ours.
    body = collection_notice()
    notice = provider.parse_notice(signed(body), body)
    assert notice == Notice("payaza", "karo-col_1:Funds Received:2026-10-20T10:00:00", "karo-col_1", None)


def test_a_notice_signed_with_another_secret_is_rejected(provider):
    # Bug caught: anyone who can reach the webhook URL posting a payment notice.
    body = collection_notice()
    assert provider.parse_notice(signed(body, "some-other-secret"), body) is None


def test_a_notice_whose_body_was_changed_after_signing_is_rejected(provider):
    # Bug caught: a signed notice being edited in flight to point at another collection.
    body = collection_notice()
    headers = signed(body)
    assert provider.parse_notice(headers, collection_notice(merchant_reference="karo-col_2")) is None


def test_a_notice_with_no_signature_or_a_non_ascii_one_is_rejected_without_an_error(provider):
    # Bug caught: a missing header accepted, or hmac.compare_digest raising on a non-ASCII header and taking the
    # webhook down.
    body = collection_notice()
    assert provider.parse_notice({}, body) is None
    assert provider.parse_notice({"x-payaza-signature": "café"}, body) is None


def test_the_signature_header_is_found_whatever_its_case(provider):
    # Bug caught: a server that capitalises header names making every real notice look forged.
    body = collection_notice()
    headers = {"X-Payaza-Signature": signed(body)["x-payaza-signature"]}
    assert provider.parse_notice(headers, body) is not None


def test_a_signed_notice_that_is_not_a_collection_or_payout_is_ignored(provider):
    # Bug caught: a signed body of some other shape being read as a payment.
    for body in (b"not json", b"[1, 2]", json.dumps({"merchant_reference": "karo-col_1"}).encode()):
        assert provider.parse_notice(signed(body), body) is None


def test_a_payout_notice_names_the_payout_reference_and_no_collection(provider):
    # Bug caught: a payout notice credited as a collection.
    body = json.dumps(
        {
            "transaction_reference": "karo-prop_1",
            "transaction_status": "NIP_SUCCESS",
            "sent_to": {"account_name": "Nakato Supplies", "account_number": "256700000002", "bank_name": "MTN"},
        }
    ).encode()
    notice = provider.parse_notice(signed(body), body)
    assert notice == Notice("payaza", "karo-prop_1:NIP_SUCCESS:", None, "karo-prop_1")


def test_two_statuses_of_one_collection_are_two_events(provider):
    # Bug caught: a "Transaction Failed" notice swallowed as a repeat of "Funds Received".
    paid, failed = collection_notice(), collection_notice(transaction_status="Transaction Failed", status="Failed")
    first, second = provider.parse_notice(signed(paid), paid), provider.parse_notice(signed(failed), failed)
    assert first.event_id != second.event_id


def test_a_replayed_notice_has_the_same_event_id(provider):
    # Bug caught: a replayed notice looking new, so it is processed twice.
    body = collection_notice()
    assert provider.parse_notice(signed(body), body).event_id == provider.parse_notice(signed(body), body).event_id


# --- a payout -------------------------------------------------------------------------------------------------------


def pay(provider, payee=UGANDAN_WALLET, **changes):
    request = {
        "reference": "karo-prop_1",
        "amount": ugx("50000"),
        "debit_currency": "KES",
        "payee": payee,
        "narration": "Fees - term 1!",
    } | changes
    return provider.payout(**request)


def test_a_payout_is_sent_to_the_wallet_in_the_documented_shape_and_is_pending(payaza, provider):
    # Bug caught: a payout Payaza rejects for its shape (a "+" in the number, a punctuated narration, an amount that
    # differs between payout_amount and credit_amount), or one read as done before Payaza says so.
    route = payaza.post(PAYOUT).mock(return_value=httpx.Response(200, json=PAYOUT_ACCEPTED))
    result = pay(provider, AIRTEL_WALLET)
    assert (result.status, result.provider_id, result.reference) == ("pending", "karo-prop_1", "karo-prop_1")
    sent = json.loads(route.calls.last.request.content)
    assert sent["transaction_type"] == "mobile_money"
    service = sent["service_payload"]
    assert (service["currency"], service["country"], service["account_reference"]) == (
        "UGX",
        "UGA",
        "made-up-ugx-account",
    )
    assert (service["payout_amount"], service["transaction_pin"]) == (50000, 123456)
    (beneficiary,) = service["payout_beneficiaries"]
    assert beneficiary["credit_amount"] == service["payout_amount"]
    assert (beneficiary["account_number"], beneficiary["bank_code"]) == ("256750000003", "AIRUGA")
    assert (beneficiary["narration"], beneficiary["transaction_reference"]) == ("Fees  term 1", "karo-prop_1")
    assert beneficiary["sender"]["sender_name"] == "Karo Family"
    assert route.calls.last.request.headers["x-tenantid"] == "test"
    assert "x-productid" not in route.calls.last.request.headers


def test_mtn_wallets_use_the_mtn_code(payaza, provider):
    # Bug caught: an MTN wallet paid through the Airtel rail.
    route = payaza.post(PAYOUT).mock(return_value=httpx.Response(200, json=PAYOUT_ACCEPTED))
    pay(provider)
    sent = json.loads(route.calls.last.request.content)
    assert sent["service_payload"]["payout_beneficiaries"][0]["bank_code"] == "MTNUGA"


@pytest.mark.parametrize("status", [200, 500])
def test_an_explicit_refusal_is_failed_even_when_payaza_sends_it_with_a_500(payaza, provider, status):
    # Bug caught: a payout Payaza refused (no balance) held pending forever, or the refusal lost behind a 500.
    body = {"response_code": 500, "response_message": "Insufficient Balance", "resp_code": "500"}
    payaza.post(PAYOUT).mock(return_value=httpx.Response(status, json=body))
    result = pay(provider)
    assert result.status == "failed"
    assert "Insufficient Balance" in result.message


def test_a_wrong_pin_and_a_wrong_bank_code_are_refusals(payaza, provider):
    # Bug caught: a refusal made before any transfer existed being left pending for reconcile.
    payaza.post(PAYOUT).mock(return_value=httpx.Response(200, json={"response_message": "Invalid transaction pin"}))
    assert pay(provider).status == "failed"
    payaza.post(PAYOUT).mock(return_value=httpx.Response(200, json={"resp_code": "X02", "response_message": ""}))
    assert pay(provider).status == "failed"


def test_an_answer_that_is_not_a_known_refusal_is_a_connection_error_not_failed(payaza, provider):
    # Bug caught: any odd payout answer refunding money that may already be on its way.
    payaza.post(PAYOUT).mock(return_value=httpx.Response(500, json={"response_message": "Something went wrong"}))
    with pytest.raises(ConnectionError, match="not a known refusal"):
        pay(provider)
    payaza.post(PAYOUT).mock(return_value=httpx.Response(200, json={"response_content": "oops"}))
    with pytest.raises(ConnectionError):
        pay(provider)


def test_a_reference_payaza_already_has_is_pending_not_failed(payaza, provider):
    # Bug caught: a retried payout whose first call landed read as a failure, refunding money that left.
    body = {"response_code": 0, "response_message": "Transaction reference already exists.", "resp_code": "X03"}
    payaza.post(PAYOUT).mock(return_value=httpx.Response(200, json=body))
    result = pay(provider)
    assert (result.status, result.provider_id) == ("pending", None)


@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
def test_a_payout_with_no_answer_is_a_connection_error(payaza, provider, failure):
    # Bug caught: a timeout read as a failed payout, refunding money that may have left.
    payaza.post(PAYOUT).mock(side_effect=failure)
    with pytest.raises(ConnectionError):
        pay(provider)


@pytest.mark.parametrize(
    "request_changes",
    [
        {"payee": KENYAN_WALLET},
        {"payee": UGANDAN_WALLET, "amount": Money.parse("50", "GBP")},
        {"payee": Payee.of("Nakato", country="UG", currency="UGX", method="bank", phone="256700000002")},
    ],
)
def test_a_payout_payaza_is_not_enabled_for_is_failed_with_nothing_sent(payaza, provider, request_changes):
    # Bug caught: a Kenyan wallet or a pound payout sent down the UGX route, or an invented endpoint.
    result = pay(provider, **request_changes)
    assert result.status == "failed"
    assert "Nothing was sent" in result.message
    assert not payaza.calls


def test_a_payout_without_the_pin_account_and_sender_is_failed_with_nothing_sent(payaza, unconfigured):
    # Bug caught: a payout sent to Payaza without a PIN or account, which it would refuse or mishandle.
    result = pay(unconfigured)
    assert (result.status, "Nothing was sent" in result.message) == ("failed", True)
    assert not payaza.calls


# --- checking a payout ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NIP_SUCCESS", "succeeded"),
        ("NIP_FAILURE", "failed"),
        ("TRANSACTION_INITIATED", "pending"),
        ("NIP_PENDING", "pending"),
        ("ESCROW_SUCCESS", "pending"),
        ("A_STATUS_PAYAZA_ADDS_LATER", "pending"),
    ],
)
def test_each_payout_status_maps_and_an_unknown_one_is_pending(payaza, provider, raw, expected):
    # Bug caught: ESCROW_SUCCESS (still reversible) read as final, or a status Payaza adds later read as failed and
    # refunded.
    route = payaza.get(STATUS + "karo-prop_1").mock(return_value=httpx.Response(200, json=payout_state(raw)))
    result = provider.payout_status(reference="karo-prop_1", provider_id=None)
    assert (result.status, result.reference) == (expected, "karo-prop_1")
    assert route.called


def test_a_failed_payout_carries_payazas_reason(payaza, provider):
    # Bug caught: a failed payout shown with no reason.
    body = payout_state("NIP_FAILURE", responseMessage="Wallet not active")
    payaza.get(STATUS + "karo-prop_1").mock(return_value=httpx.Response(200, json=body))
    assert "Wallet not active" in provider.payout_status(reference="karo-prop_1", provider_id="karo-prop_1").message


def test_a_payout_payaza_cannot_find_is_pending_not_failed(payaza, provider):
    # Bug caught: a payout still being created, or one not yet visible, read as failed and refunded while it leaves.
    payaza.get(STATUS + "karo-prop_1").mock(return_value=httpx.Response(404, json={"message": "not found"}))
    result = provider.payout_status(reference="karo-prop_1", provider_id=None)
    assert (result.status, result.provider_id) == ("pending", None)


@pytest.mark.parametrize("code", [400, 401, 403, 429, 500])
def test_a_payout_status_with_no_usable_answer_raises(payaza, provider, code):
    # Bug caught: an auth or rate-limit answer read as "no record", or as a verdict on the payout.
    payaza.get(STATUS + "karo-prop_1").mock(return_value=httpx.Response(code, json={"message": "no"}))
    with pytest.raises(ConnectionError):
        provider.payout_status(reference="karo-prop_1", provider_id=None)


def test_a_payout_status_with_no_transaction_status_raises(payaza, provider):
    # Bug caught: an empty answer read as a status.
    payaza.get(STATUS + "karo-prop_1").mock(return_value=httpx.Response(200, json={"data": {}}))
    with pytest.raises(ConnectionError, match="transactionStatus"):
        provider.payout_status(reference="karo-prop_1", provider_id=None)


def test_a_reference_is_encoded_in_the_status_path(payaza, provider):
    # Bug caught: a reference with a slash or "?" turning into a different request path.
    route = payaza.get(url__regex=r".*/transaction/.*").mock(
        return_value=httpx.Response(200, json=payout_state("NIP_PENDING"))
    )
    provider.payout_status(reference="a/b?c", provider_id=None)
    assert route.calls.last.request.url.raw_path.endswith(b"/transaction/a%2Fb%3Fc")


# --- quotes ---------------------------------------------------------------------------------------------------------


def test_a_quote_in_one_currency_is_one_to_one(provider):
    # Bug caught: a same-currency payment refused because Payaza has no rate API.
    quote = provider.quote("UGX", ugx("90000"))
    assert (quote.source, quote.target, quote.rate, quote.provider) == (
        ugx("90000"),
        ugx("90000"),
        Decimal(1),
        "payaza",
    )


def test_a_cross_currency_quote_is_refused_not_invented(provider):
    # Bug caught: Karo showing a made-up Payaza rate as if Payaza had priced it.
    with pytest.raises(PayazaNotEnabled, match="no rate API"):
        provider.quote("KES", ugx("90000"))


# --- the keys -------------------------------------------------------------------------------------------------------


def test_a_live_or_missing_key_is_refused():
    # Bug caught: a live key reaching an entry that must only move test money, or a missing key running unsigned.
    with pytest.raises(RuntimeError, match="test key"):
        PayazaProvider("PZ78-PKLIVE-made-up-0000", SECRET_KEY)
    with pytest.raises(RuntimeError, match="PAYAZA_PUBLIC_KEY"):
        PayazaProvider("", SECRET_KEY)
    with pytest.raises(RuntimeError, match="PAYAZA_SECRET_KEY"):
        PayazaProvider(PUBLIC_KEY, "")


# --- composing and switching on -------------------------------------------------------------------------------------


def test_the_hybrid_collects_on_payaza_and_quotes_and_pays_on_the_stand_in():
    # Bug caught: payouts or quotes silently going to Payaza, which has no rates and is not enabled for transfers here.
    built = payaza_hybrid(PUBLIC_KEY, SECRET_KEY, base_url="https://karo.example")
    assert isinstance(built, HybridProvider)
    assert (built.name, payout_name(built)) == ("payaza", "standin")
    quote = built.quote("KES", ugx("31600"))
    assert isinstance(quote, ProviderQuote)
    assert (quote.provider, quote.source) == ("standin", kes("1000"))


def test_the_default_provider_is_still_the_stand_in():
    # Bug caught: the Payaza wiring changing the setup the demo is recorded on.
    assert isinstance(build_provider(Settings(standin_secret="made-up-standin-secret")), StandInProvider)


def test_payaza_without_its_keys_is_refused_at_start_naming_them():
    # Bug caught: KARO_PROVIDER=payaza starting with no keys, then failing on the first payment.
    with pytest.raises(RuntimeError, match="PAYAZA_PUBLIC_KEY.*PAYAZA_SECRET_KEY"):
        build_provider(Settings(provider="payaza"))
    with pytest.raises(RuntimeError, match="PAYAZA_SECRET_KEY"):
        build_provider(Settings(provider="payaza", payaza_public_key=PUBLIC_KEY))


def test_payaza_with_a_live_key_is_refused_at_start():
    # Bug caught: a live key in .env reaching a running app, which could then move real money.
    live = Settings(provider="payaza", payaza_public_key="PZ78-PKLIVE-made-up-0000", payaza_secret_key=SECRET_KEY)
    with pytest.raises(RuntimeError, match="test key"):
        build_provider(live)


def test_payaza_with_its_keys_builds_the_hybrid():
    # Bug caught: KARO_PROVIDER=payaza quietly running on the stand-in.
    keyed = Settings(provider="payaza", payaza_public_key=PUBLIC_KEY, payaza_secret_key=SECRET_KEY)
    built = build_provider(keyed)
    assert (built.name, payout_name(built)) == ("payaza", "standin")


def test_the_payaza_settings_are_read_and_kept_out_of_the_repr(monkeypatch):
    # Bug caught: the keys not read from .env, or a log line showing them.
    for name, value in {
        "KARO_PIN_PEPPER": "p-xyz",
        "KARO_SESSION_SECRET": "s-xyz",
        "KARO_DEMO_PASSWORD": "d-xyz",
        "KARO_PROVIDER": "payaza",
        "PAYAZA_PUBLIC_KEY": PUBLIC_KEY,
        "PAYAZA_SECRET_KEY": SECRET_KEY,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("KARO_STANDIN_SECRET", raising=False)
    settings = load_settings(None)
    assert (settings.provider, settings.payaza_public_key, settings.payaza_secret_key) == (
        "payaza",
        PUBLIC_KEY,
        SECRET_KEY,
    )
    assert PUBLIC_KEY not in repr(settings) and SECRET_KEY not in repr(settings)
