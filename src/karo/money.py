"""Money as whole minor units of one currency. Decimal places come from CLDR, not from code."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import cache, total_ordering

from babel.numbers import format_currency, get_currency_precision, list_currencies


class CurrencyMismatch(ValueError):
    pass


@cache
def _known_currencies() -> frozenset[str]:
    return frozenset(list_currencies())


def decimals(currency: str) -> int:
    return get_currency_precision(currency)


@total_ordering
@dataclass(frozen=True)
class Money:
    minor: int
    currency: str

    def __post_init__(self) -> None:
        if type(self.minor) is not int:
            raise TypeError(f"minor units must be an int, got {type(self.minor).__name__}")
        if self.currency not in _known_currencies():
            raise ValueError(f"unknown currency {self.currency!r}")

    @classmethod
    def parse(cls, amount: str | int | float | Decimal, currency: str) -> Money:
        """Read a human amount such as "6000.00". Refuses more decimals than the currency has."""
        places = decimals(currency)
        scaled = Decimal(str(amount)).scaleb(places)
        if scaled != scaled.to_integral_value():
            raise ValueError(f"{currency} has {places} decimal places; got {amount}")
        return cls(int(scaled), currency)

    @property
    def decimal(self) -> Decimal:
        return Decimal(self.minor).scaleb(-decimals(self.currency))

    def format(self, locale: str = "en") -> str:
        return format_currency(self.decimal, self.currency, locale=locale)

    def _same(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError(f"expected Money, got {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatch(f"{self.currency} and {other.currency} can't be combined")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.minor - other.minor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._same(other)
        return self.minor < other.minor

    def __str__(self) -> str:
        return f"{self.currency} {self.decimal:,}"
