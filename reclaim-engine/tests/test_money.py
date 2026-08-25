"""Phase 1 tests — the Money primitive.

These tests are the contract for RECLAIM's correctness bedrock. They assert
both the happy path and every failure mode we care about (no float, no
currency mixing, immutability, exact decimal math).
"""
from decimal import Decimal

import pytest

from reclaim.money import (
    Money,
    CurrencyMismatchError,
    InvalidMoneyError,
    minor_units_for,
    register_currency,
)


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------
def test_of_accepts_str_int_decimal():
    assert Money.of("100.50", "INR").amount == Decimal("100.50")
    assert Money.of(100, "INR").amount == Decimal("100")
    assert Money.of(Decimal("99.99"), "INR").amount == Decimal("99.99")


def test_of_rejects_float():
    with pytest.raises(InvalidMoneyError):
        Money.of(0.1, "INR")


def test_of_rejects_bool():
    with pytest.raises(InvalidMoneyError):
        Money.of(True, "INR")


def test_of_rejects_unparseable_string():
    with pytest.raises(InvalidMoneyError):
        Money.of("not-a-number", "INR")


def test_raw_constructor_rejects_non_decimal():
    with pytest.raises(InvalidMoneyError):
        Money(0.1, "INR")  # type: ignore[arg-type]
    with pytest.raises(InvalidMoneyError):
        Money(100, "INR")  # type: ignore[arg-type]  # int must go through .of


def test_rejects_non_finite():
    with pytest.raises(InvalidMoneyError):
        Money.of("NaN", "INR")
    with pytest.raises(InvalidMoneyError):
        Money.of("Infinity", "INR")


@pytest.mark.parametrize("bad", ["inr", "IN", "INRR", "1NR", "", "US$"])
def test_rejects_bad_currency(bad):
    with pytest.raises(InvalidMoneyError):
        Money.of("1", bad)


def test_zero():
    z = Money.zero("INR")
    assert z.is_zero
    assert z.amount == Decimal("0")


# --------------------------------------------------------------------------
# The classic float bug is impossible with Money
# --------------------------------------------------------------------------
def test_no_float_error_in_addition():
    total = Money.of("0.10", "INR") + Money.of("0.20", "INR")
    assert total == Money.of("0.30", "INR")
    # And to be explicit about what we avoided:
    assert (0.1 + 0.2) != 0.3          # the float world is broken
    assert total.amount == Decimal("0.30")  # the Money world is exact


# --------------------------------------------------------------------------
# Equality, precision, hashing
# --------------------------------------------------------------------------
def test_equality_ignores_trailing_zeros():
    assert Money.of("1.1", "INR") == Money.of("1.10", "INR")


def test_equality_requires_same_currency():
    assert Money.of("1", "INR") != Money.of("1", "USD")


def test_equality_with_non_money_is_false():
    assert (Money.of("1", "INR") == 1) is False
    assert (Money.of("1", "INR") == "1 INR") is False


def test_hash_consistent_for_equal_values():
    a = Money.of("1.1", "INR")
    b = Money.of("1.10", "INR")
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1  # usable in a set as one element


def test_usable_as_dict_key():
    d = {Money.of("5.00", "INR"): "five"}
    assert d[Money.of("5", "INR")] == "five"


# --------------------------------------------------------------------------
# Arithmetic
# --------------------------------------------------------------------------
def test_add_and_sub():
    assert Money.of("10", "INR") + Money.of("2.50", "INR") == Money.of("12.50", "INR")
    assert Money.of("10", "INR") - Money.of("2.50", "INR") == Money.of("7.50", "INR")


def test_add_currency_mismatch_raises():
    with pytest.raises(CurrencyMismatchError):
        Money.of("1", "INR") + Money.of("1", "USD")


def test_sub_currency_mismatch_raises():
    with pytest.raises(CurrencyMismatchError):
        Money.of("1", "INR") - Money.of("1", "USD")


def test_negation_and_abs():
    assert -Money.of("5", "INR") == Money.of("-5", "INR")
    assert abs(Money.of("-5", "INR")) == Money.of("5", "INR")


def test_multiply_by_int_and_decimal():
    assert Money.of("100", "INR") * 3 == Money.of("300", "INR")
    assert Money.of("100", "INR") * Decimal("0.02") == Money.of("2.00", "INR")
    assert 3 * Money.of("100", "INR") == Money.of("300", "INR")  # rmul


def test_multiply_rejects_float_and_bool():
    with pytest.raises(InvalidMoneyError):
        Money.of("100", "INR") * 0.02
    with pytest.raises(InvalidMoneyError):
        Money.of("100", "INR") * True


def test_multiply_preserves_full_precision():
    # 2% MDR on 4783.20 = 95.664 — precision preserved until explicit rounding.
    fee = Money.of("4783.20", "INR") * Decimal("0.02")
    assert fee.amount == Decimal("95.6640")
    assert not fee.is_rounded()


# --------------------------------------------------------------------------
# Rounding
# --------------------------------------------------------------------------
def test_round_half_up_to_minor_units():
    assert Money.of("95.664", "INR").round() == Money.of("95.66", "INR")
    assert Money.of("95.665", "INR").round() == Money.of("95.67", "INR")  # half-up


def test_round_respects_currency_minor_units():
    # JPY has 0 minor units.
    assert Money.of("123.9", "JPY").round() == Money.of("124", "JPY")


def test_is_rounded():
    assert Money.of("10.00", "INR").is_rounded()
    assert Money.of("10", "INR").is_rounded()
    assert not Money.of("10.001", "INR").is_rounded()


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------
def test_ordering_same_currency():
    assert Money.of("1", "INR") < Money.of("2", "INR")
    assert Money.of("2", "INR") > Money.of("1", "INR")
    assert Money.of("1", "INR") <= Money.of("1.00", "INR")
    assert Money.of("1", "INR") >= Money.of("1.00", "INR")


def test_ordering_currency_mismatch_raises():
    with pytest.raises(CurrencyMismatchError):
        Money.of("1", "INR") < Money.of("2", "USD")


# --------------------------------------------------------------------------
# Immutability
# --------------------------------------------------------------------------
def test_money_is_frozen():
    m = Money.of("1", "INR")
    with pytest.raises(Exception):
        m.amount = Decimal("2")  # type: ignore[misc]


def test_operations_return_new_instances():
    a = Money.of("10", "INR")
    b = a + Money.of("5", "INR")
    assert a == Money.of("10", "INR")  # original unchanged
    assert b == Money.of("15", "INR")


# --------------------------------------------------------------------------
# Currency registry
# --------------------------------------------------------------------------
def test_minor_units_default_and_registered():
    assert minor_units_for("INR") == 2
    assert minor_units_for("JPY") == 0
    assert minor_units_for("XYZ") == 2  # default


def test_register_currency():
    register_currency("BHD", 3)
    assert minor_units_for("BHD") == 3
    assert Money.of("1.234", "BHD").round() == Money.of("1.234", "BHD")


def test_register_currency_validates():
    with pytest.raises(InvalidMoneyError):
        register_currency("bhd", 3)
    with pytest.raises(InvalidMoneyError):
        register_currency("BHD", -1)


# --------------------------------------------------------------------------
# Representation
# --------------------------------------------------------------------------
def test_str_and_repr():
    m = Money.of("1234.50", "INR")
    assert str(m) == "1234.50 INR"
    assert repr(m) == "Money('1234.50', 'INR')"


# --------------------------------------------------------------------------
# Additional branch coverage (foundation must be fully exercised)
# --------------------------------------------------------------------------
def test_of_rejects_unsupported_type():
    with pytest.raises(InvalidMoneyError):
        Money.of([1, 2], "INR")  # type: ignore[arg-type]
    with pytest.raises(InvalidMoneyError):
        Money.of(None, "INR")  # type: ignore[arg-type]


def test_sign_properties():
    assert Money.of("5", "INR").is_positive
    assert not Money.of("5", "INR").is_negative
    assert Money.of("-5", "INR").is_negative
    assert not Money.of("-5", "INR").is_positive
    assert not Money.zero("INR").is_positive
    assert not Money.zero("INR").is_negative


def test_arithmetic_with_non_money_raises_typeerror():
    with pytest.raises(TypeError):
        Money.of("1", "INR") + 1  # type: ignore[operator]
    with pytest.raises(TypeError):
        Money.of("1", "INR") - 1  # type: ignore[operator]
    with pytest.raises(TypeError):
        Money.of("1", "INR") < 1  # type: ignore[operator]


def test_multiply_by_non_numeric_is_typeerror():
    # __mul__ returns NotImplemented for unsupported types -> Python raises TypeError.
    with pytest.raises(TypeError):
        Money.of("1", "INR") * "2"  # type: ignore[operator]
