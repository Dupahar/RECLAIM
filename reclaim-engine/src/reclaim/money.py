"""Money — an exact, currency-safe monetary value object.

The bedrock of RECLAIM's correctness (goal **G1**). Money is stored as a
``decimal.Decimal`` and is **never** a binary float: floats cannot represent
decimal currency exactly (``0.1 + 0.2 != 0.3``), which is unacceptable for a
system that moves money. See ADR-0001.

Design rules enforced here:
- The only accepted inputs are ``int``, ``str`` and ``Decimal``. ``float`` and
  ``bool`` are rejected loudly at construction.
- Money is immutable (frozen). Every operation returns a new ``Money``.
- Arithmetic between different currencies raises ``CurrencyMismatchError``.
- Amounts may carry more precision than the currency's minor units during
  intermediate math (e.g. a fee of 2% of 4783.20); ``round()`` snaps to the
  currency's minor units when a final, postable amount is needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Union

# Explicitly NOT float. This alias documents the accepted input types.
Amount = Union[int, str, Decimal]

# Minor-unit registry (ISO-4217 style). Extend via ``register_currency``.
_MINOR_UNITS: dict[str, int] = {
    "INR": 2, "USD": 2, "EUR": 2, "GBP": 2, "AED": 2, "SGD": 2, "JPY": 0,
}
_DEFAULT_MINOR_UNITS = 2


class MoneyError(Exception):
    """Base class for all money errors."""


class CurrencyMismatchError(MoneyError):
    """Raised when combining or comparing money of different currencies."""


class InvalidMoneyError(MoneyError):
    """Raised for invalid amounts or currencies."""


def register_currency(code: str, minor_units: int) -> None:
    """Register or override a currency's minor-unit precision."""
    if not (isinstance(code, str) and len(code) == 3 and code.isalpha() and code.isupper()):
        raise InvalidMoneyError(f"currency code must be 3 uppercase letters, got {code!r}")
    if not isinstance(minor_units, int) or isinstance(minor_units, bool) or minor_units < 0:
        raise InvalidMoneyError(f"minor_units must be a non-negative int, got {minor_units!r}")
    _MINOR_UNITS[code] = minor_units


def minor_units_for(currency: str) -> int:
    """Return the number of decimal places for a currency (default 2)."""
    return _MINOR_UNITS.get(currency, _DEFAULT_MINOR_UNITS)


def _validate_currency(currency: str) -> None:
    if not (isinstance(currency, str) and len(currency) == 3 and currency.isalpha() and currency.isupper()):
        raise InvalidMoneyError(f"currency must be a 3-letter uppercase code, got {currency!r}")


@dataclass(frozen=True, eq=False)
class Money:
    """An immutable (amount, currency) pair backed by ``Decimal``."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        # Guard the raw constructor too, so Money(Decimal(...), "INR") is safe
        # and Money(0.1, "INR") / Money(True, "INR") fail loudly.
        if isinstance(self.amount, bool) or not isinstance(self.amount, Decimal):
            raise InvalidMoneyError(
                f"amount must be a Decimal (use Money.of for int/str), got {type(self.amount).__name__}"
            )
        if not self.amount.is_finite():
            raise InvalidMoneyError(f"amount must be finite, got {self.amount!r}")
        _validate_currency(self.currency)

    # ---- construction -------------------------------------------------
    @classmethod
    def of(cls, amount: Amount, currency: str) -> "Money":
        """Build Money from an int, str, or Decimal. ``float``/``bool`` rejected."""
        if isinstance(amount, bool):
            raise InvalidMoneyError("bool is not a valid money amount")
        if isinstance(amount, float):
            raise InvalidMoneyError(
                "float is forbidden for money (precision loss); pass str, int, or Decimal"
            )
        if isinstance(amount, Decimal):
            dec = amount
        elif isinstance(amount, int):
            dec = Decimal(amount)
        elif isinstance(amount, str):
            try:
                dec = Decimal(amount)
            except InvalidOperation as exc:
                raise InvalidMoneyError(f"could not parse amount {amount!r}") from exc
        else:
            raise InvalidMoneyError(f"unsupported amount type {type(amount).__name__}")
        return cls(dec, currency)

    @classmethod
    def zero(cls, currency: str) -> "Money":
        return cls(Decimal("0"), currency)

    # ---- properties ---------------------------------------------------
    @property
    def minor_units(self) -> int:
        return minor_units_for(self.currency)

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    def is_rounded(self) -> bool:
        """True if the amount has no more decimal places than the currency allows."""
        exponent = self.amount.as_tuple().exponent
        # exponent is an int for finite Decimals; negative means fractional digits.
        return (-exponent) <= self.minor_units if isinstance(exponent, int) else False

    # ---- rounding -----------------------------------------------------
    def round(self, rounding: str = ROUND_HALF_UP) -> "Money":
        """Snap the amount to the currency's minor units (default ROUND_HALF_UP)."""
        quantum = Decimal(1).scaleb(-self.minor_units)  # 2 -> 0.01, 0 -> 1
        return Money(self.amount.quantize(quantum, rounding=rounding), self.currency)

    # ---- arithmetic ---------------------------------------------------
    def _same_currency(self, other: "Money") -> None:
        if not isinstance(other, Money):
            raise TypeError(f"expected Money, got {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatchError(f"{self.currency} vs {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self._same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount, self.currency)

    def __abs__(self) -> "Money":
        return Money(abs(self.amount), self.currency)

    def __mul__(self, factor: Union[int, Decimal]) -> "Money":
        if isinstance(factor, bool) or isinstance(factor, float):
            raise InvalidMoneyError("multiply Money by int or Decimal only (not float/bool)")
        if not isinstance(factor, (int, Decimal)):
            return NotImplemented
        return Money(self.amount * factor, self.currency)

    __rmul__ = __mul__

    # ---- equality / hashing / ordering --------------------------------
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.currency == other.currency and self.amount == other.amount

    def __hash__(self) -> int:
        # normalize() so that 1.10 and 1.1 (which are ==) hash identically.
        return hash((self.currency, self.amount.normalize()))

    def __lt__(self, other: "Money") -> bool:
        self._same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        self._same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        self._same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: "Money") -> bool:
        self._same_currency(other)
        return self.amount >= other.amount

    # ---- representation ----------------------------------------------
    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"

    def __repr__(self) -> str:
        return f"Money({str(self.amount)!r}, {self.currency!r})"
