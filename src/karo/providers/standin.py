"""The stand-in provider: the whole demo and every test run on it, with no network and no real money.

State lives in memory on the instance, so the web app keeps one instance for the life of its process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Mapping
from decimal import ROUND_CEILING, Decimal, localcontext
from pathlib import Path
from typing import TYPE_CHECKING

from karo.money import Money, decimals
from karo.providers.base import Collection, CollectionStatus, Notice, PayoutResult, ProviderQuote

if TYPE_CHECKING:
    from karo.domain import Payee  # a name for the annotations only; see base.py

SIGNATURE_HEADER = "x-standin-signature"

# src/karo/providers/standin.py -> repo root. Found from the package, so it works from any working directory.
_RATES_PATH = Path(__file__).resolve().parents[3] / "config" / "standin_rates.json"


class StandInProvider:
    name = "standin"

    def __init__(self, *, rates: Mapping[tuple[str, str], Decimal], secret: str, base_url: str) -> None:
        self._rates = dict(rates)
        self._secret = secret.encode()
        self._base_url = base_url
        self._amounts: dict[str, Money] = {}
        self._paid: set[str] = set()
        self._payouts: dict[str, PayoutResult] = {}
        self._payout_lock = threading.Lock()
        self.payout_calls = 0

    @classmethod
    def from_config(cls, secret: str, base_url: str, path: str | Path | None = None) -> StandInProvider:
        source = Path(path) if path is not None else _RATES_PATH
        raw = json.loads(source.read_text(encoding="utf-8"))["rates"]
        rates: dict[tuple[str, str], Decimal] = {}
        for pair, rate in raw.items():
            source_currency, target_currency = pair.split("->")
            rates[(source_currency, target_currency)] = Decimal(rate)
        return cls(rates=rates, secret=secret, base_url=base_url)

    def quote(self, source_currency: str, target: Money) -> ProviderQuote:
        if source_currency == target.currency:
            return ProviderQuote(Money(target.minor, source_currency), target, Decimal(1), self.name)
        rate = self._rates.get((source_currency, target.currency))
        if rate is None:
            raise ValueError(f"No stand-in rate for {source_currency} to {target.currency}.")
        # The target is fixed. Round the source up, so the payee never gets less than the target.
        with localcontext() as context:
            context.prec = 60
            source_major = target.decimal / rate
            source_minor = source_major.scaleb(decimals(source_currency)).to_integral_value(ROUND_CEILING)
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
        # A repeat call must not turn an already-paid collection back into a pending one.
        self._amounts.setdefault(tx_ref, amount)
        return Collection(tx_ref, f"{self._base_url}/standin/pay/{tx_ref}", self.name)

    def mark_paid(self, tx_ref: str) -> tuple[dict[str, str], bytes]:
        """Pay a collection and return the notice, headers and body, as this provider would post it to the web app."""
        if tx_ref not in self._amounts:
            raise KeyError(tx_ref)
        self._paid.add(tx_ref)
        # The event id depends only on the tx_ref, so a replayed notice is recognisably the same event.
        body = json.dumps({"event_id": f"SI-EVT-{tx_ref}", "tx_ref": tx_ref}, sort_keys=True).encode()
        return {SIGNATURE_HEADER: self._sign(body)}, body

    def verify_collection(self, tx_ref: str) -> CollectionStatus:
        amount = self._amounts.get(tx_ref)
        if amount is None:
            return CollectionStatus(tx_ref, "failed", None, None)
        if tx_ref in self._paid:
            return CollectionStatus(tx_ref, "succeeded", amount, f"SI-{tx_ref}")
        return CollectionStatus(tx_ref, "pending", amount, None)

    def payout(
        self, *, reference: str, amount: Money, debit_currency: str, payee: Payee, narration: str
    ) -> PayoutResult:
        # Held across the check and the store: two callers with one reference must make one payout.
        with self._payout_lock:
            stored = self._payouts.get(reference)
            if stored is not None:
                return stored
            self.payout_calls += 1
            result = PayoutResult(
                reference, f"SI-{reference}", "succeeded", True, "Simulated payout. No real money moved."
            )
            self._payouts[reference] = result
            return result

    def payout_status(self, *, reference: str, provider_id: str | None) -> PayoutResult:
        stored = self._payouts.get(reference)
        if stored is not None:
            return stored
        # Not "failed": a payout still being sent isn't visible yet either. Only a provider that says so fails one.
        return PayoutResult(reference, None, "pending", True, "not found")

    def parse_notice(self, headers: Mapping[str, str], body: bytes) -> Notice | None:
        sent = {name.lower(): value for name, value in headers.items()}.get(SIGNATURE_HEADER)
        # Compare as bytes: compare_digest raises on a str that is not ASCII.
        if sent is None or not hmac.compare_digest(sent.encode(), self._sign(body).encode()):
            return None
        try:
            data = json.loads(body)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("event_id"), str):
            return None
        return Notice(self.name, data["event_id"], data.get("tx_ref"), data.get("reference"))

    def _sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()
