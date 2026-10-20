"""One provider for money in and another for money out, behind the same interface.

Used when Flutterwave takes the payments but its account cannot make transfers: the stand-in quotes and pays out,
and says so on every row it writes.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import TYPE_CHECKING

from karo.money import Money
from karo.providers.base import Collection, CollectionStatus, Notice, PayoutResult, Provider, ProviderQuote
from karo.providers.flutterwave import FlutterwaveProvider
from karo.providers.standin import StandInProvider

if TYPE_CHECKING:
    from karo.domain import Payee  # a name for the annotations only; see base.py


class HybridProvider:
    """`name` is the collecting side's, because that is whose notices arrive at /webhooks/<name>.

    A quote goes with the payouts, because it prices them. A collection's currency conversion asks for one too, so a
    fee and the shares that fund it are priced from the same rates.
    """

    def __init__(self, *, collections: Provider, payouts: Provider) -> None:
        self._collections = collections
        self._payouts = payouts

    @property
    def name(self) -> str:
        return self._collections.name

    @property
    def payout_name(self) -> str:
        return self._payouts.name

    def quote(self, source_currency: str, target: Money) -> ProviderQuote:
        return self._payouts.quote(source_currency, target)

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
        return self._collections.create_collection(
            tx_ref=tx_ref,
            amount=amount,
            method=method,
            payer_name=payer_name,
            payer_email=payer_email,
            payer_phone=payer_phone,
            redirect_url=redirect_url,
        )

    def verify_collection(self, tx_ref: str) -> CollectionStatus:
        return self._collections.verify_collection(tx_ref)

    def payout(
        self, *, reference: str, amount: Money, debit_currency: str, payee: Payee, narration: str
    ) -> PayoutResult:
        return self._payouts.payout(
            reference=reference, amount=amount, debit_currency=debit_currency, payee=payee, narration=narration
        )

    def payout_status(self, *, reference: str, provider_id: str | None) -> PayoutResult:
        return self._payouts.payout_status(reference=reference, provider_id=provider_id)

    def parse_notice(self, headers: Mapping[str, str], body: bytes) -> Notice | None:
        return self._collections.parse_notice(headers, body)


def flutterwave_hybrid(secret_key: str, webhook_hash: str, *, payouts: str, base_url: str) -> HybridProvider:
    """Flutterwave takes the payments. `payouts` is "standin" (simulated, the default) or "flutterwave"."""
    flutterwave = FlutterwaveProvider(secret_key, webhook_hash)
    if payouts == "flutterwave":
        return HybridProvider(collections=flutterwave, payouts=flutterwave)
    if payouts != "standin":
        raise RuntimeError(f"KARO_FLW_PAYOUTS={payouts!r} is not one of 'standin' or 'flutterwave'.")
    # Here the stand-in only quotes and pays out. It never reads a notice, so its signing secret is never used.
    return HybridProvider(
        collections=flutterwave, payouts=StandInProvider.from_config(secrets.token_urlsafe(32), base_url)
    )
