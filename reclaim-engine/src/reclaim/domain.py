"""Canonical domain model for RECLAIM.

Immutable value objects that everything downstream (ledger, reconciliation,
recovery) shares. All monetary fields are :class:`~reclaim.money.Money`;
validation and invariants are enforced in ``__post_init__`` rather than left to
convention. IDs and timestamps are *provided by the caller* (never generated
internally) so the domain stays deterministic and replayable (goal **G4**).

Mirrors the schema sketch in ``../RECLAIM-System-Architecture.md`` Appendix B.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from .money import Money


class DomainError(Exception):
    """Raised when a domain object violates an invariant."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DomainError(message)


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------
class Source(str, Enum):
    ORDERS = "orders"
    SETTLEMENT = "settlement"
    BANK = "bank"
    INVOICE = "invoice"


class Direction(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class MatchStatus(str, Enum):
    UNMATCHED = "unmatched"
    MATCHED = "matched"
    EXCEPTION = "exception"


class LeakType(str, Enum):
    FAILED_DEBIT = "failed_debit"
    SHORT_PAYMENT = "short_payment"
    MISSING_SETTLEMENT = "missing_settlement"
    ABANDONED_CHECKOUT = "abandoned_checkout"
    UNEXPLAINED_FEE = "unexplained_fee"
    TIMING = "timing"
    OVERDUE_RECEIVABLE = "overdue_receivable"


class RecoveryState(str, Enum):
    NONE = "none"
    PENDING = "pending"
    RECOVERED = "recovered"
    EXHAUSTED = "exhausted"
    NOT_RECOVERABLE = "not_recoverable"
    HALTED = "halted"  # stopped mid-recovery; needs a human (e.g. executor error / unknown cause)


def _validate_confidence(value: Optional[float], label: str) -> None:
    if value is None:
        return
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool),
        f"{label} must be a number in [0,1]",
    )
    _require(0.0 <= float(value) <= 1.0, f"{label} must be within [0,1], got {value}")


# --------------------------------------------------------------------------
# Fees — the stacked deductions on a settlement (MDR, GST-on-MDR, TCS, other)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Fees:
    mdr: Money
    gst_on_mdr: Money
    tcs: Money
    other: Money

    def __post_init__(self) -> None:
        parts = (self.mdr, self.gst_on_mdr, self.tcs, self.other)
        for m in parts:
            _require(isinstance(m, Money), "every fee component must be Money")
        currencies = {m.currency for m in parts}
        _require(len(currencies) == 1, f"all fees must share one currency, got {currencies}")
        for m in parts:
            _require(not m.is_negative, f"fee components must be non-negative, got {m}")

    @property
    def currency(self) -> str:
        return self.mdr.currency

    def total(self) -> Money:
        return self.mdr + self.gst_on_mdr + self.tcs + self.other

    @classmethod
    def zero(cls, currency: str) -> "Fees":
        z = Money.zero(currency)
        return cls(z, z, z, z)


# --------------------------------------------------------------------------
# Transaction — one normalized record from any source
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TransactionRefs:
    order_id: Optional[str] = None
    utr: Optional[str] = None
    rrn: Optional[str] = None
    invoice_no: Optional[str] = None


@dataclass(frozen=True)
class Transaction:
    id: str
    source: Source
    gross_amount: Money
    ts: datetime
    fees: Optional[Fees] = None
    refs: TransactionRefs = field(default_factory=TransactionRefs)
    counterparty: Optional[str] = None
    narration_raw: Optional[str] = None
    match_status: MatchStatus = MatchStatus.UNMATCHED
    match_confidence: Optional[float] = None

    def __post_init__(self) -> None:
        _require(isinstance(self.id, str) and self.id != "", "transaction id is required")
        _require(isinstance(self.source, Source), "source must be a Source")
        _require(isinstance(self.gross_amount, Money), "gross_amount must be Money")
        _require(isinstance(self.ts, datetime), "ts must be a datetime")
        _require(isinstance(self.match_status, MatchStatus), "match_status must be a MatchStatus")
        _require(isinstance(self.refs, TransactionRefs), "refs must be TransactionRefs")
        if self.fees is not None:
            _require(isinstance(self.fees, Fees), "fees must be Fees")
            _require(
                self.fees.currency == self.gross_amount.currency,
                "fees currency must match gross_amount currency",
            )
        _validate_confidence(self.match_confidence, "match_confidence")

    @property
    def currency(self) -> str:
        return self.gross_amount.currency

    @property
    def net_amount(self) -> Money:
        """Amount after deductions (gross - total fees). Equals gross if no fees."""
        return self.gross_amount - self.fees.total() if self.fees is not None else self.gross_amount


# --------------------------------------------------------------------------
# LedgerEntry — one side of a double-entry posting
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LedgerEntry:
    id: str
    txn_id: str
    account: str
    direction: Direction
    amount: Money
    ts: datetime

    def __post_init__(self) -> None:
        _require(isinstance(self.id, str) and self.id != "", "ledger entry id is required")
        _require(isinstance(self.txn_id, str) and self.txn_id != "", "txn_id is required")
        _require(isinstance(self.account, str) and self.account != "", "account is required")
        _require(isinstance(self.direction, Direction), "direction must be a Direction")
        _require(isinstance(self.amount, Money), "amount must be Money")
        # Ledger amounts are always positive magnitudes; sign is carried by direction.
        _require(self.amount.is_positive, "ledger amount must be positive (sign is the direction)")
        # Postable amounts must be at the currency's minor units — no sub-unit dust.
        _require(self.amount.is_rounded(), "ledger amount must be at currency minor units")
        _require(isinstance(self.ts, datetime), "ts must be a datetime")


# --------------------------------------------------------------------------
# LeakRecord — a rupee that failed to reconcile
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LeakRecord:
    id: str
    amount: Money
    leak_type: LeakType
    source_refs: tuple[str, ...] = ()
    hypothesis: str = ""
    confidence: Optional[float] = None
    recoverable: bool = False
    recovery_state: RecoveryState = RecoveryState.NONE

    def __post_init__(self) -> None:
        _require(isinstance(self.id, str) and self.id != "", "leak id is required")
        _require(isinstance(self.amount, Money), "amount must be Money")
        _require(self.amount.is_positive, "leak amount must be a positive magnitude")
        _require(isinstance(self.leak_type, LeakType), "leak_type must be a LeakType")
        _require(isinstance(self.source_refs, tuple), "source_refs must be a tuple")
        _require(all(isinstance(r, str) for r in self.source_refs), "source_refs must be strings")
        _require(isinstance(self.recoverable, bool), "recoverable must be bool")
        _require(isinstance(self.recovery_state, RecoveryState), "recovery_state must be a RecoveryState")
        _validate_confidence(self.confidence, "confidence")
