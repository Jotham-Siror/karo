"""Flutterwave v3, test mode only.

How a reply is read, because it decides whether money is refunded or credited:
- No answer (a dropped connection, a timeout, a 5xx, a bad key, a rate limit, an unreadable body) raises
  `ConnectionError`. The executor leaves the payout pending and a later check settles it. Marking it `failed` here
  would refund money that may already be on its way.
- Flutterwave reports some of its own failures as an HTTP 400 too (see proof/smoke/), so a 400 on a payout is not a
  verdict by itself. Only a message on `_PAYOUT_REFUSALS`, a refusal made before any transfer exists, makes a payout
  `failed`. Any other error answer raises `ConnectionError`, which leaves the payout pending for reconcile.
- A payout with no transfer id (its first answer was lost) is looked up with `GET /transfers?reference=<reference as
  sent>`, whose `data` is a list of transfers. One match settles it, none stays pending, and two or an unreadable
  reply raise `ConnectionError`.
- A request Flutterwave refuses before anything happens (no rate, a bad payment option) raises `ValueError`, the
  error the stand-in raises when it has no price.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from typing import TYPE_CHECKING, Any

import httpx

from karo import countries
from karo.money import Money, decimals
from karo.providers.base import Collection, CollectionStatus, Notice, PayoutResult, ProviderQuote, Status

if TYPE_CHECKING:
    from karo.domain import Payee  # a name for the annotations only; see base.py

BASE_URL = "https://api.flutterwave.com/v3"
TEST_KEY_PREFIX = "FLWSECK_TEST-"
WEBHOOK_HEADER = "verif-hash"
# Test mode succeeds a transfer whose reference ends like this.
TEST_MODE_SUCCESS = "_PMCK"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# Messages that refuse a transfer before one exists, seen in proof/smoke/. Add one only on evidence: the cost of a
# message missing here is a payout held pending, the cost of a wrong one is a refund of money that left.
_PAYOUT_REFUSALS = ("not enabled to make transfers", "ip whitelisting")
# Replies that say nothing about the payment itself: a bad key, a request timeout, a rate limit.
_NO_VERDICT = frozenset({401, 403, 408, 429})
_TRANSFER_STATUS: dict[str, Status] = {
    "NEW": "pending",
    "PENDING": "pending",
    "SUCCESSFUL": "succeeded",
    "FAILED": "failed",
}


def require_test_key(secret_key: str) -> str:
    """Karo's entry only ever moves test-mode money. Refuse anything that isn't a test key."""
    if not secret_key:
        raise RuntimeError("FLW_SECRET_KEY is not set. Add your Flutterwave test secret key to .env.")
    if not secret_key.startswith(TEST_KEY_PREFIX):
        raise RuntimeError(f"Refusing to run: FLW_SECRET_KEY is not a test key (must start {TEST_KEY_PREFIX}).")
    return secret_key


class FlutterwaveProvider:
    name = "flutterwave"

    def __init__(self, secret_key: str, webhook_hash: str) -> None:
        key = require_test_key(secret_key)
        if not webhook_hash:
            # An empty hash would match an empty header, and anyone could post a payment notice.
            raise RuntimeError("FLW_WEBHOOK_HASH is not set. Add the dashboard's secret hash to .env.")
        self._webhook_hash = webhook_hash.encode()
        self._client = httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)

    def quote(self, source_currency: str, target: Money) -> ProviderQuote:
        if source_currency == target.currency:
            return ProviderQuote(Money(target.minor, source_currency), target, Decimal(1), self.name)
        pair = f"{source_currency} to {target.currency}"
        code, body = self._send(
            "GET",
            "/transfers/rates",
            params={
                "amount": format(target.decimal, "f"),
                "source_currency": source_currency,
                "destination_currency": target.currency,
            },
        )
        data = _data(code, body)
        if data is None:
            raise ValueError(f"Flutterwave has no rate for {pair}: {_message(body)}")
        source_major = _priced(data, source_currency, target)
        if source_major is None:
            raise ValueError(f"Flutterwave's rate for {pair} is not for this payment.")
        with localcontext() as context:
            context.prec = 60
            source_minor = source_major.scaleb(decimals(source_currency)).to_integral_value(ROUND_CEILING)
            context.prec = 12
            rate = target.decimal / source_major
        # The target is fixed, so the source is rounded up: the payee never gets less.
        return ProviderQuote(Money(int(source_minor), source_currency), target, rate, self.name)

    def create_collection(
        self,
        *,
        tx_ref: str,
        amount: Money,
        method: str,
        payer_name: str,
        payer_email: str,
        payer_phone: str | None,
        redirect_url: str,
    ) -> Collection:
        customer = {"email": payer_email, "name": payer_name}
        if payer_phone:
            customer["phonenumber"] = payer_phone
        payload = {
            "tx_ref": tx_ref,
            "amount": _major(amount),
            "currency": amount.currency,
            "redirect_url": redirect_url,
            "payment_options": _payment_option(method),
            "customer": customer,
        }
        code, body = self._send("POST", "/payments", json=payload)
        data = _data(code, body)
        if data is None or not data.get("link"):
            raise ValueError(f"Flutterwave would not make a payment link: {_message(body)}")
        return Collection(tx_ref, data["link"], self.name)

    def verify_collection(self, tx_ref: str) -> CollectionStatus:
        code, body = self._send("GET", "/transactions/verify_by_reference", params={"tx_ref": tx_ref})
        data = _data(code, body)
        if data is None:
            if _not_found(body):
                # A hosted link makes no transaction until someone tries to pay, so an unpaid link is not a lost one.
                return CollectionStatus(tx_ref, "pending", None, None)
            raise ValueError(f"Flutterwave would not check {tx_ref}: {_message(body)}")
        amount = _paid_amount(data)
        if data.get("status") != "successful":
            # Not "failed" even when an attempt failed: the hosted link stays payable, and a replacement link would
            # leave a later payment on the old one with nowhere to be credited.
            return CollectionStatus(tx_ref, "pending", amount, None)
        if amount is None:
            raise ValueError(f"Flutterwave called {tx_ref} successful without an amount.")
        return CollectionStatus(tx_ref, "succeeded", amount, str(data["id"]))

    def payout(
        self, *, reference: str, amount: Money, debit_currency: str, payee: Payee, narration: str
    ) -> PayoutResult:
        account = _account_fields(payee)
        if account is None:
            message = "Karo can't pay that kind of account through Flutterwave. Nothing was sent."
            return PayoutResult(reference, None, "failed", True, message)
        payload = {
            **account,
            "amount": _major(amount),
            "currency": amount.currency,
            "debit_currency": debit_currency,
            "narration": narration,
            "reference": reference + TEST_MODE_SUCCESS,
        }
        code, body = self._send("POST", "/transfers", json=payload)
        data = _data(code, body)
        if data is not None:
            return _transfer_result(reference, data)
        if _is_duplicate(body):
            # The first call landed and only its answer was lost. Failing this would refund money that left.
            return PayoutResult(reference, None, "pending", True, "Flutterwave already has this reference.")
        if _is_known_refusal(body):
            return PayoutResult(reference, None, "failed", True, f"Flutterwave refused the payout: {_message(body)}")
        raise ConnectionError(f"Flutterwave's payout answer is not a known refusal (HTTP {code}): {_message(body)}")

    def payout_status(self, *, reference: str, provider_id: str | None) -> PayoutResult:
        # Not "failed" for anything Flutterwave cannot find: a transfer still being created isn't visible yet.
        not_found = PayoutResult(reference, None, "pending", True, "not found")
        if provider_id is None:
            return self._transfer_by_reference(reference, not_found)
        code, body = self._send("GET", f"/transfers/{provider_id}")
        data = _data(code, body)
        if data is not None:
            return _transfer_result(reference, data)
        if code == 404 or _not_found(body):
            return not_found
        raise ConnectionError(f"Flutterwave gave no usable status for transfer {provider_id} (HTTP {code}).")

    def _transfer_by_reference(self, reference: str, not_found: PayoutResult) -> PayoutResult:
        """A payout whose first answer was lost has no transfer id: ask under the reference it was sent as."""
        sent_as = reference + TEST_MODE_SUCCESS
        code, body = self._send("GET", "/transfers", params={"reference": sent_as})
        found = body.get("data")
        if code < 400 and body.get("status") == "success" and isinstance(found, list):
            # The filter is Flutterwave's, but the id taken from this answer settles money, so match it again here.
            ours = [item for item in found if isinstance(item, dict) and item.get("reference") == sent_as]
            if len(ours) == 1:
                return _transfer_result(reference, ours[0])
            if not ours:
                return not_found
        elif code == 404 or _not_found(body):
            return not_found
        raise ConnectionError(f"Flutterwave gave no usable transfer list for {reference} (HTTP {code}).")

    def parse_notice(self, headers: Mapping[str, str], body: bytes) -> Notice | None:
        sent = {name.lower(): value for name, value in headers.items()}.get(WEBHOOK_HEADER)
        # Compare as bytes: compare_digest raises on a str that is not ASCII.
        if sent is None or not hmac.compare_digest(sent.encode(), self._webhook_hash):
            return None
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or data.get("id") is None:
            return None
        return Notice(self.name, str(data["id"]), _text(data.get("tx_ref")), _text(data.get("reference")))

    def _send(self, method: str, path: str, **options: Any) -> tuple[int, dict]:
        try:
            response = self._client.request(method, path, **options)
        except httpx.HTTPError as error:
            raise ConnectionError(f"Flutterwave could not be reached ({type(error).__name__}).") from error
        code = response.status_code
        if code >= 500 or code in _NO_VERDICT:
            raise ConnectionError(f"Flutterwave gave no answer to act on (HTTP {code}).")
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ConnectionError(f"Flutterwave sent a reply that can't be read (HTTP {code}).")
        return code, body


def _data(code: int, body: dict) -> dict | None:
    """The `data` of a reply that says it worked, else None."""
    data = body.get("data")
    return data if code < 400 and body.get("status") == "success" and isinstance(data, dict) else None


def _message(body: dict) -> str:
    return str(body.get("message") or "no reason given")[:200]


def _not_found(body: dict) -> bool:
    message = _message(body).lower()
    return "not found" in message or "no transaction" in message or "no transfer" in message


def _is_known_refusal(body: dict) -> bool:
    message = _message(body).lower()
    return any(refusal in message for refusal in _PAYOUT_REFUSALS)


def _is_duplicate(body: dict) -> bool:
    message = _message(body).lower()
    return "duplicate" in message or ("reference" in message and "already" in message)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _major(amount: Money) -> int | float:
    """The amount in major units as a JSON number, whole when it is whole (UGX 5000 goes as 5000)."""
    value = amount.decimal
    return int(value) if value == value.to_integral_value() else float(value)


def _payment_option(method: str) -> str:
    for country in countries.load().values():
        for collect in country.collect_methods:
            if collect.id == method:
                return collect.flutterwave_option
    raise ValueError(f"Karo has no Flutterwave payment option for the method {method!r}.")


def _priced(data: dict, source_currency: str, target: Money) -> Decimal | None:
    """What the family pays, in major units, if the reply is the rate asked for. None if it is for something else."""
    try:
        source, destination = data["source"], data["destination"]
        if (source["currency"], destination["currency"]) != (source_currency, target.currency):
            return None
        if Decimal(str(destination["amount"])) != target.decimal:
            return None
        cost = Decimal(str(source["amount"]))
    except (KeyError, TypeError, InvalidOperation):
        return None
    return cost if cost > 0 else None


def _paid_amount(data: dict) -> Money | None:
    if data.get("amount") is None or data.get("currency") is None:
        return None
    return Money.parse(data["amount"], data["currency"])


def _account_fields(payee: Payee) -> dict[str, Any] | None:
    """Where the transfer goes, for the two kinds of account this adapter can pay. None for any other."""
    details = dict(payee.details)
    if details.get("country") == "GB" and "sort_code" in details and "account_number" in details:
        return {
            "account_number": details["account_number"],
            "beneficiary_name": payee.name,
            "meta": [
                {
                    "routing_number": details["sort_code"],
                    "bank_name": details.get("bank_name", payee.name),
                    "account_type": details.get("account_type", "corporate"),
                    "beneficiary_merchant_name": payee.name,
                }
            ],
        }
    if details.get("country") == "KE" and "phone" in details:
        return {"account_bank": "MPS", "account_number": details["phone"], "beneficiary_name": payee.name}
    return None


def _transfer_result(reference: str, data: dict) -> PayoutResult:
    status = _TRANSFER_STATUS.get(str(data.get("status")).upper(), "pending")
    provider_id = None if data.get("id") is None else str(data["id"])
    message = f"Simulated payout (Flutterwave test mode). No real money moved. Transfer {status}."
    return PayoutResult(reference, provider_id, status, True, message)
