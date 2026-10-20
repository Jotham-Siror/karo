"""Payaza adapter, ported from a working integration in another project (alianza), where Payaza's NGN card checkout
ran in test mode. Switched off in Karo (KARO_PROVIDER=payaza). Not yet run against Payaza from Karo. KES, UGX and XAF
collections and payouts need Payaza to enable them for the account.

What is ported (endpoints, headers and field names are alianza's src/providers/payaza.ts, which cites Payaza's docs):
- a KES M-Pesa collection: a prompt is pushed to the payer's phone (the link Karo hands out is its own /paid page, since
  Payaza documents no hosted KES checkout), and its status is read back by our reference;
- a UGX mobile-money payout (MTN or Airtel) and its status by reference;
- the webhook signature: base64(HMAC-SHA512(secret key, raw body)) in `x-payaza-signature`.

Left out, because nothing in Karo can use it or Payaza documents no counterpart: the NGN card popup (it needs a web
page of ours to host Payaza's script, and Karo collects no NGN) and its transaction-query status; Payaza has no
documented rate API, so `quote` only answers a same-currency payment (the hybrid asks the payout side for rates anyway).

How a reply is read, as in flutterwave.py, because it decides whether money is refunded or credited:
- No answer (a dropped connection, a timeout, a bad key, a rate limit, an unreadable body) raises `ConnectionError`.
  The executor leaves the payout pending and a later check settles it.
- A payout is `failed` only on a refusal Payaza made before any transfer exists (no balance, a wrong PIN, a wrong bank
  code), or when nothing was sent at all. A no-balance refusal counts only if Payaza has no record of the reference
  (otherwise an earlier call landed and the record's status is returned). Any other answer raises `ConnectionError`,
  and a reference Payaza cannot find or does not recognise stays `pending`.
- A collection is `succeeded` only when the transaction's own status says completed, never on the envelope code.
- Known gap: the M-Pesa prompt is sent inside `create_collection`, before collections.py stores the row, so a failed
  insert leaves a prompt Karo cannot credit. It cannot be closed inside this adapter.
- Anything this account is not set up for raises `PayazaNotEnabled` (a `ValueError`, what the stand-in raises when it
  has no price) before a request is made; a payout the same way comes back `failed` with "Nothing was sent".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import quote as url_quote

import httpx

from karo.money import Money
from karo.providers.base import Collection, CollectionStatus, Notice, PayoutResult, ProviderQuote, Status
from karo.providers.hybrid import HybridProvider
from karo.providers.standin import StandInProvider

if TYPE_CHECKING:
    from karo.domain import Payee  # a name for the annotations only; see base.py

BASE_URL = "https://api.payaza.africa/live"
# Key formats come from Payaza's doc examples (PZ78-PKTEST-..., PZ78-PKLIVE-...). Not seen on a real key from Karo.
TEST_KEY_MARKER = "-PKTEST-"
SIGNATURE_HEADER = "x-payaza-signature"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# Replies that say nothing about the payment itself: a bad key, a request timeout, a rate limit.
_NO_VERDICT = frozenset({401, 403, 408, 429})
_MOBILE_MONEY_PENDING = frozenset({"09", "00"})
_MOBILE_MONEY_FAILED = frozenset({"06", "96"})
_NETWORK_CODE = {"mtn_ug": "MTNUGA", "airtel_ug": "AIRUGA"}
# ESCROW_SUCCESS is debited but still with the destination rail and reversible, so it is not final.
_PAYOUT_STATUS: dict[str, Status] = {
    "NIP_SUCCESS": "succeeded",
    "NIP_FAILURE": "failed",
    "TRANSACTION_INITIATED": "pending",
    "NIP_PENDING": "pending",
    "ESCROW_SUCCESS": "pending",
}
# Messages that refuse a payout before one exists, from alianza's adapter. Add one only on evidence: the cost of a
# message missing here is a payout held pending, the cost of a wrong one is a refund of money that left.
_PIN_REFUSAL = re.compile(r"invalid transaction pin", re.IGNORECASE)
# Unlike a wrong PIN, this can follow a payout that landed (the first call emptied the balance, its answer was lost, the
# re-send is refused), so it is believed only after Payaza is asked about the reference.
_BALANCE_REFUSAL = re.compile(r"insufficient balance", re.IGNORECASE)


class PayazaNotEnabled(ValueError):
    """Karo asked for something Payaza has not enabled for this account, or that alianza never had an endpoint for."""


@dataclass(frozen=True)
class PayazaSender:
    """The sender Payaza wants on a payout. Required by it; not a secret."""

    name: str
    phone: str
    address: str


def require_test_key(public_key: str) -> str:
    """Karo's entry only ever moves test-mode money. Refuse anything that isn't a test key."""
    if not public_key:
        raise RuntimeError("PAYAZA_PUBLIC_KEY is not set. Add your Payaza test public key to .env.")
    if TEST_KEY_MARKER not in public_key:
        raise RuntimeError(f"Refusing to run: PAYAZA_PUBLIC_KEY is not a test key (must contain {TEST_KEY_MARKER}).")
    return public_key


class PayazaProvider:
    name = "payaza"

    def __init__(
        self,
        public_key: str,
        secret_key: str,
        *,
        base_url: str = BASE_URL,
        transaction_pin: str | None = None,
        ugx_account_ref: str | None = None,
        sender: PayazaSender | None = None,
    ) -> None:
        key = require_test_key(public_key)
        if not secret_key:
            # An empty key would sign every body the same way, and anyone could post a payment notice.
            raise RuntimeError("PAYAZA_SECRET_KEY is not set. Add your Payaza test secret key to .env.")
        self._secret_key = secret_key.encode()
        self._transaction_pin = transaction_pin
        self._ugx_account_ref = ugx_account_ref
        self._sender = sender
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Payaza {base64.b64encode(key.encode()).decode()}",
                "X-TenantID": "test",
                "Content-Type": "application/json",
            },
            timeout=TIMEOUT,
        )

    def quote(self, source_currency: str, target: Money) -> ProviderQuote:
        if source_currency == target.currency:
            return ProviderQuote(Money(target.minor, source_currency), target, Decimal(1), self.name)
        raise PayazaNotEnabled(
            f"Payaza has no rate API, so it cannot price {source_currency} to {target.currency}. Quote with another provider."
        )

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
        if method != "mpesa" or amount.currency != "KES":
            raise PayazaNotEnabled(
                f"Payaza is not enabled for {method} collections in {amount.currency} on this account."
            )
        if not payer_phone:
            raise ValueError("Payaza's M-Pesa prompt needs the payer's phone number.")
        first, _, last = payer_name.strip().partition(" ")
        number = payer_phone.lstrip("+")
        payload = {
            "amount": _major(amount),
            "customer_number": number,
            "transaction_reference": tx_ref,
            "transaction_description": f"Karo {tx_ref}"[:100],
            "customer_bank_code": "SAFKEN",
            "currency_code": amount.currency,
            "customer_email": payer_email,
            "customer_first_name": first,
            "customer_last_name": last.strip() or first,
            "customer_phone_number": number,
            "country_code": "KE",
        }
        code, body = self._send("POST", "/subsidiary/collections/v1/process-collection", json=payload, product=True)
        answer = str(body.get("response_code", ""))
        # Only ever pending here: success is taken from verify_collection, with an amount, never from this reply.
        if answer in _MOBILE_MONEY_PENDING:
            return Collection(tx_ref, redirect_url, self.name)
        if answer in _MOBILE_MONEY_FAILED or 400 <= code < 500:
            raise ValueError(f"Payaza would not start the M-Pesa prompt: {_message(body)}")
        raise ConnectionError(
            f"Payaza's collection answer is not one to act on (HTTP {code}, code {answer or 'none'})."
        )

    def verify_collection(self, tx_ref: str) -> CollectionStatus:
        params = {"transaction_reference": tx_ref, "country_code": "KE"}
        code, body = self._send("GET", "/subsidiary/collections/v1/check-status", params=params, product=True)
        if code == 404 or _not_found(body):
            # A prompt Payaza has not recorded yet is not a lost one.
            return CollectionStatus(tx_ref, "pending", None, None)
        if code >= 400:
            raise ConnectionError(f"Payaza gave no usable status for {tx_ref} (HTTP {code}).")
        answer = str(body.get("response_code", ""))
        raw = str(body.get("transaction_status") or answer)
        amount = _amount(body.get("transaction_amount"), body.get("currency"))
        # The envelope code "00" is not read as paid: only the transaction's own status is.
        if raw in ("Completed", "Funds Received"):
            if amount is None:
                raise ValueError(f"Payaza called {tx_ref} successful without an amount.")
            return CollectionStatus(tx_ref, "succeeded", amount, _text(body.get("transaction_reference")))
        if answer in _MOBILE_MONEY_FAILED or "fail" in raw.lower():
            return CollectionStatus(tx_ref, "failed", None, None)
        return CollectionStatus(tx_ref, "pending", amount, None)

    def payout(
        self, *, reference: str, amount: Money, debit_currency: str, payee: Payee, narration: str
    ) -> PayoutResult:
        """Pays a UGX mobile-money wallet. It draws on the UGX account whatever `debit_currency` says: Payaza has no FX."""
        details = dict(payee.details)
        network = _NETWORK_CODE.get(details.get("method", ""))
        phone = details.get("phone")
        if amount.currency != "UGX" or details.get("country") != "UG" or network is None or not phone:
            return _nothing_sent(reference, "Payaza is not enabled for that kind of payout on this account.")
        if not (self._transaction_pin and self._ugx_account_ref and self._sender):
            return _nothing_sent(reference, "Payaza payouts are not set up (PIN, UGX account reference, sender).")
        value = _major(amount)
        payload = {
            "transaction_type": "mobile_money",
            "service_payload": {
                "payout_amount": value,
                "transaction_pin": int(self._transaction_pin),
                "account_reference": self._ugx_account_ref,
                "currency": "UGX",
                "country": "UGA",
                "payout_beneficiaries": [
                    {
                        "credit_amount": value,
                        "account_number": phone.lstrip("+"),
                        "account_name": payee.name,
                        "bank_code": network,
                        "narration": re.sub(r"[^A-Za-z0-9 ]", "", narration)[:25].strip(),
                        "transaction_reference": reference,
                        "sender": {
                            "sender_name": self._sender.name,
                            "sender_id": "",
                            "sender_phone_number": self._sender.phone,
                            "sender_address": self._sender.address,
                        },
                    }
                ],
            },
        }
        code, body = self._send("POST", "/payout-receptor/payout", json=payload)
        if body.get("resp_code") == "X03":
            # The first call landed and only its answer was lost. Failing this would refund money that left.
            return PayoutResult(reference, None, "pending", True, "Payaza already has this reference.")
        content = body.get("response_content")
        content = content if isinstance(content, dict) else {}
        if (
            content.get("response_status") == "TRANSACTION_INITIATED"
            or content.get("transaction_status") == "09"
            or body.get("resp_code") == "09"
        ):
            return PayoutResult(
                reference, reference, "pending", True, "Payaza has the payout (test mode). Transfer pending."
            )
        message = str(body.get("response_message") or "")
        refused = PayoutResult(
            reference, None, "failed", True, f"Payaza refused the payout: {message or body.get('resp_code')}"
        )
        if _PIN_REFUSAL.search(message):
            return refused
        if _BALANCE_REFUSAL.search(message) or body.get("resp_code") == "X02":
            # Ask Payaza about the reference first: a record means an earlier call landed, and its status is the answer.
            known = self.payout_status(reference=reference, provider_id=None)
            return refused if known.provider_id is None or known.status == "failed" else known
        raise ConnectionError(f"Payaza's payout answer is not a known refusal (HTTP {code}): {_message(body)}")

    def payout_status(self, *, reference: str, provider_id: str | None) -> PayoutResult:
        # Payaza looks a payout up by our reference, so provider_id is not needed.
        # Not "failed" for anything Payaza cannot find: a transfer still being created isn't visible yet.
        not_found = PayoutResult(reference, None, "pending", True, "not found")
        path = f"/payaza-account/api/v1/mainaccounts/merchant/transaction/{url_quote(reference, safe='')}"
        code, body = self._send("GET", path)
        if code == 404:
            return not_found
        # Auth, rate-limit or malformed answers say nothing about the payout: never read them as "no record".
        if code >= 400:
            raise ConnectionError(f"Payaza gave no usable status for payout {reference} (HTTP {code}).")
        data = body.get("data")
        raw = data.get("transactionStatus") if isinstance(data, dict) else None
        if not raw:
            raise ConnectionError(f"Payaza's payout status for {reference} had no transactionStatus.")
        # A status Payaza adds later is "pending", never "failed".
        status = _PAYOUT_STATUS.get(str(raw), "pending")
        detail = f" {_text(data.get('responseMessage')) or ''}" if status == "failed" else ""
        message = f"Payaza reports {raw}.{detail}"
        return PayoutResult(reference, reference, status, True, message)

    def parse_notice(self, headers: Mapping[str, str], body: bytes) -> Notice | None:
        sent = {name.lower(): value for name, value in headers.items()}.get(SIGNATURE_HEADER)
        if sent is None:
            return None
        expected = base64.b64encode(hmac.new(self._secret_key, body, hashlib.sha512).digest())
        # Compare as bytes: compare_digest raises on a str that is not ASCII.
        if not hmac.compare_digest(sent.strip().encode(), expected):
            return None
        try:
            data = json.loads(body)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        # Payaza webhooks carry no event type: sent_to is a payout, received_from is a collection.
        if data.get("sent_to"):
            reference = _text(data.get("transaction_reference")) or _text(data.get("merchant_reference"))
            tx_ref = None
        elif data.get("received_from"):
            # Card webhooks put Payaza's own reference in transaction_reference and ours in merchant_reference.
            tx_ref = _text(data.get("merchant_reference")) or _text(data.get("transaction_reference"))
            reference = None
        else:
            return None
        key = tx_ref or reference
        if not key:
            return None
        status = data.get("transaction_status") or data.get("status") or "unknown"
        event_id = f"{key}:{status}:{data.get('current_status_date') or ''}"
        return Notice(self.name, event_id, tx_ref, reference)

    def _send(self, method: str, path: str, *, product: bool = False, **options: Any) -> tuple[int, dict]:
        headers = {"X-ProductID": "app"} if product else {}
        try:
            response = self._client.request(method, path, headers=headers, **options)
        except httpx.HTTPError as error:
            raise ConnectionError(f"Payaza could not be reached ({type(error).__name__}).") from error
        code = response.status_code
        if code in _NO_VERDICT:
            raise ConnectionError(f"Payaza gave no answer to act on (HTTP {code}).")
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ConnectionError(f"Payaza sent a reply that can't be read (HTTP {code}).")
        # A 5xx with a readable body is returned, not raised: Payaza reports some refusals with a 500 code, and each
        # caller decides what a body it does not recognise means.
        return code, body


def payaza_hybrid(public_key: str, secret_key: str, *, base_url: str) -> HybridProvider:
    """Payaza takes the payments. The stand-in quotes and pays out, and says so on every row it writes."""
    payaza = PayazaProvider(public_key, secret_key)
    # Here the stand-in only quotes and pays out. It never reads a notice, so its signing secret is never used.
    return HybridProvider(collections=payaza, payouts=StandInProvider.from_config(secrets.token_urlsafe(32), base_url))


def _nothing_sent(reference: str, why: str) -> PayoutResult:
    return PayoutResult(reference, None, "failed", True, f"{why} Nothing was sent.")


def _message(body: dict) -> str:
    return str(body.get("response_message") or body.get("message") or "no reason given")[:200]


def _not_found(body: dict) -> bool:
    return re.search(r"not\s*found", _message(body), re.IGNORECASE) is not None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _major(amount: Money) -> int | float:
    """The amount in major units as a JSON number, whole when it is whole (UGX 5000 goes as 5000)."""
    value = amount.decimal
    return int(value) if value == value.to_integral_value() else float(value)


def _amount(value: object, currency: object) -> Money | None:
    if value is None or not isinstance(currency, str):
        return None
    try:
        return Money.parse(str(value), currency)
    except (ValueError, InvalidOperation):
        return None
