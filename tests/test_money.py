from decimal import Decimal

import pytest

from karo.money import CurrencyMismatch, Money


def test_ugx_has_no_decimal_places():
    # Bug caught: assuming two decimals everywhere would store UGX 1,500 as 150,000.
    assert Money.parse("1500", "UGX").minor == 1500
    with pytest.raises(ValueError):
        Money.parse("1500.50", "UGX")


def test_gbp_and_kes_have_two_decimal_places():
    assert Money.parse("6000.00", "GBP").minor == 600_000
    assert Money.parse("990000", "KES").minor == 99_000_000
    with pytest.raises(ValueError):
        Money.parse("0.005", "GBP")


def test_unknown_currency_is_rejected():
    with pytest.raises(ValueError):
        Money(100, "XYZ")


def test_fractional_minor_units_are_rejected():
    with pytest.raises(TypeError):
        Money(1.5, "GBP")


def test_mixing_currencies_raises_instead_of_adding():
    # Bug caught: a cap check that silently adds KES to GBP.
    with pytest.raises(CurrencyMismatch):
        Money(100, "KES") + Money(100, "GBP")
    with pytest.raises(CurrencyMismatch):
        Money(100, "KES") > Money(100, "GBP")


def test_same_currency_arithmetic_and_ordering():
    a, b = Money(250, "KES"), Money(100, "KES")
    assert a + b == Money(350, "KES")
    assert a - b == Money(150, "KES")
    assert b < a and a >= b and not a < b


def test_decimal_value_and_format_follow_the_currency():
    assert Money(600_000, "GBP").decimal == Decimal("6000.00")
    assert Money(600_000, "GBP").format("en_GB") == "£6,000.00"
    assert "." not in Money(1500, "UGX").format("en_UG")


def test_xaf_has_no_decimal_places():
    # Bug caught: assuming two decimals would store XAF 5,000 as 500,000 and send a hundred times too much.
    assert Money.parse("5000", "XAF").minor == 5000
    assert str(Money.parse("5000", "XAF")) == "XAF 5,000"
    with pytest.raises(ValueError):
        Money.parse("5000.50", "XAF")
