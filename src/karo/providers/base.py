"""What Karo needs from a payment provider. The stand-in and the Flutterwave adapter both implement this.

Nothing here touches our database: a provider only talks money in and money out.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from karo.money import Money

if TYPE_CHECKING:
    # karo.domain pulls in the clock, which pulls in the database. Names only, so keep it out of the runtime imports.
    from karo.domain import Payee

Status = Literal["pending", "succeeded", "failed"]


@dataclass(frozen=True)
class ProviderQuote:
    """The family pays `source`, the payee gets `target`. `rate` is target units per 1 source unit, in major units."""

    source: Money
    target: Money
    rate: Decimal
    provider: str


@dataclass(frozen=True)
class Collection:
    tx_ref: str
    link: str
    provider: str


@dataclass(frozen=True)
class CollectionStatus:
    tx_ref: str
    status: Status
    amount: Money | None
    provider_id: str | None


@dataclass(frozen=True)
class PayoutResult:
    reference: str
    provider_id: str | None
    status: Status
    simulated: bool
    message: str


@dataclass(frozen=True)
class Notice:
    """A provider's "something happened" message, once its sender has been checked."""

    provider: str
    event_id: str
    tx_ref: str | None
    reference: str | None


@runtime_checkable
class Provider(Protocol):
    name: str

    def quote(self, source_currency: str, target: Money) -> ProviderQuote: ...

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
    ) -> Collection: ...

    def verify_collection(self, tx_ref: str) -> CollectionStatus: ...

    def payout(
        self, *, reference: str, amount: Money, debit_currency: str, payee: Payee, narration: str
    ) -> PayoutResult: ...

    def payout_status(self, *, reference: str, provider_id: str | None) -> PayoutResult: ...

    def parse_notice(self, headers: Mapping[str, str], body: bytes) -> Notice | None:
        """None unless the notice is genuinely from this provider."""
        ...
