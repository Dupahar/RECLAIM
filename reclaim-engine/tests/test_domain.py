"""Phase 2 tests — canonical domain model."""
from datetime import datetime
from decimal import Decimal

import pytest

from reclaim.money import Money
from reclaim.domain import (
    DomainError,
    Direction,
    Fees,
    LeakRecord,
    LeakType,
    LedgerEntry,
    MatchStatus,
    RecoveryState,
    Source,
    Transaction,
    TransactionRefs,
)

TS = datetime(2026, 8, 25, 12, 0, 0)
INR = "INR"


def inr(x: str) -> Money:
    return Money.of(x, INR)


# --------------------------------------------------------------------------
# Fees
# --------------------------------------------------------------------------
def test_fees_total():
    fees = Fees(inr("95.66"), inr("17.22"), inr("5.00"), inr("0"))
    assert fees.total() == inr("117.88")
    assert fees.currency == INR


def test_fees_zero():
    fees = Fees.zero(INR)
    assert fees.total() == Money.zero(INR)


def test_fees_currency_mismatch_rejected():
    with pytest.raises(DomainError):
        Fees(inr("1"), Money.of("1", "USD"), inr("1"), inr("1"))


def test_fees_negative_rejected():
    with pytest.raises(DomainError):
        Fees(inr("-1"), inr("0"), inr("0"), inr("0"))


def test_fees_requires_money():
    with pytest.raises(DomainError):
        Fees(1, 0, 0, 0)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Transaction
# --------------------------------------------------------------------------
def test_transaction_net_equals_gross_without_fees():
    txn = Transaction(id="t1", source=Source.ORDERS, gross_amount=inr("500.00"), ts=TS)
    assert txn.net_amount == inr("500.00")
    assert txn.currency == INR
    assert txn.match_status == MatchStatus.UNMATCHED


def test_transaction_net_is_gross_minus_fees():
    # Settlement: gross 5000, fees total 117.88 -> payout 4882.12
    fees = Fees(inr("95.66"), inr("17.22"), inr("5.00"), inr("0"))
    txn = Transaction(
        id="s1", source=Source.SETTLEMENT, gross_amount=inr("5000.00"), ts=TS, fees=fees
    )
    assert txn.net_amount == inr("4882.12")


def test_transaction_fees_currency_must_match_gross():
    fees = Fees.zero("USD")
    with pytest.raises(DomainError):
        Transaction(id="s1", source=Source.SETTLEMENT, gross_amount=inr("100"), ts=TS, fees=fees)


def test_transaction_requires_nonempty_id():
    with pytest.raises(DomainError):
        Transaction(id="", source=Source.ORDERS, gross_amount=inr("1"), ts=TS)


def test_transaction_requires_money_gross():
    with pytest.raises(DomainError):
        Transaction(id="t1", source=Source.ORDERS, gross_amount=100, ts=TS)  # type: ignore[arg-type]


def test_transaction_requires_datetime():
    with pytest.raises(DomainError):
        Transaction(id="t1", source=Source.ORDERS, gross_amount=inr("1"), ts="2026-08-25")  # type: ignore[arg-type]


def test_transaction_confidence_bounds():
    Transaction(id="t1", source=Source.ORDERS, gross_amount=inr("1"), ts=TS, match_confidence=0.5)
    with pytest.raises(DomainError):
        Transaction(id="t2", source=Source.ORDERS, gross_amount=inr("1"), ts=TS, match_confidence=1.5)
    with pytest.raises(DomainError):
        Transaction(id="t3", source=Source.ORDERS, gross_amount=inr("1"), ts=TS, match_confidence=True)  # type: ignore[arg-type]


def test_transaction_refs_default_and_custom():
    txn = Transaction(id="t1", source=Source.BANK, gross_amount=inr("1"), ts=TS)
    assert txn.refs == TransactionRefs()
    txn2 = Transaction(
        id="t2", source=Source.BANK, gross_amount=inr("1"), ts=TS,
        refs=TransactionRefs(utr="UTR123"),
    )
    assert txn2.refs.utr == "UTR123"


def test_transaction_is_immutable():
    txn = Transaction(id="t1", source=Source.ORDERS, gross_amount=inr("1"), ts=TS)
    with pytest.raises(Exception):
        txn.id = "t2"  # type: ignore[misc]


# --------------------------------------------------------------------------
# LedgerEntry
# --------------------------------------------------------------------------
def test_ledger_entry_valid():
    e = LedgerEntry(id="e1", txn_id="t1", account="merchant_payable", direction=Direction.CREDIT,
                    amount=inr("100.00"), ts=TS)
    assert e.direction == Direction.CREDIT
    assert e.amount == inr("100.00")


def test_ledger_entry_amount_must_be_positive():
    with pytest.raises(DomainError):
        LedgerEntry(id="e1", txn_id="t1", account="a", direction=Direction.DEBIT,
                    amount=inr("0"), ts=TS)
    with pytest.raises(DomainError):
        LedgerEntry(id="e2", txn_id="t1", account="a", direction=Direction.DEBIT,
                    amount=inr("-5"), ts=TS)


def test_ledger_entry_amount_must_be_rounded():
    # Sub-minor-unit "dust" is not postable.
    with pytest.raises(DomainError):
        LedgerEntry(id="e1", txn_id="t1", account="a", direction=Direction.DEBIT,
                    amount=inr("100.001"), ts=TS)


def test_ledger_entry_requires_fields():
    with pytest.raises(DomainError):
        LedgerEntry(id="", txn_id="t1", account="a", direction=Direction.DEBIT,
                    amount=inr("1.00"), ts=TS)
    with pytest.raises(DomainError):
        LedgerEntry(id="e1", txn_id="", account="a", direction=Direction.DEBIT,
                    amount=inr("1.00"), ts=TS)
    with pytest.raises(DomainError):
        LedgerEntry(id="e1", txn_id="t1", account="", direction=Direction.DEBIT,
                    amount=inr("1.00"), ts=TS)


# --------------------------------------------------------------------------
# LeakRecord
# --------------------------------------------------------------------------
def test_leak_record_valid():
    leak = LeakRecord(
        id="L1", amount=inr("499.00"), leak_type=LeakType.FAILED_DEBIT,
        source_refs=("order-1187",), hypothesis="temporary insufficient balance",
        confidence=0.8, recoverable=True, recovery_state=RecoveryState.PENDING,
    )
    assert leak.recoverable is True
    assert leak.recovery_state == RecoveryState.PENDING


def test_leak_amount_must_be_positive():
    with pytest.raises(DomainError):
        LeakRecord(id="L1", amount=inr("0"), leak_type=LeakType.TIMING)
    with pytest.raises(DomainError):
        LeakRecord(id="L2", amount=inr("-5"), leak_type=LeakType.TIMING)


def test_leak_source_refs_must_be_string_tuple():
    with pytest.raises(DomainError):
        LeakRecord(id="L1", amount=inr("1"), leak_type=LeakType.TIMING, source_refs=("ok", 5))  # type: ignore[arg-type]


def test_leak_confidence_bounds():
    with pytest.raises(DomainError):
        LeakRecord(id="L1", amount=inr("1"), leak_type=LeakType.TIMING, confidence=2.0)


def test_leak_defaults():
    leak = LeakRecord(id="L1", amount=inr("1"), leak_type=LeakType.UNEXPLAINED_FEE)
    assert leak.source_refs == ()
    assert leak.recoverable is False
    assert leak.recovery_state == RecoveryState.NONE
    assert leak.confidence is None


# --------------------------------------------------------------------------
# Enums are stable string values (important for serialization/audit)
# --------------------------------------------------------------------------
def test_enum_values():
    assert Source.SETTLEMENT.value == "settlement"
    assert Direction.DEBIT.value == "debit"
    assert LeakType.FAILED_DEBIT.value == "failed_debit"
    assert RecoveryState.RECOVERED.value == "recovered"


# --------------------------------------------------------------------------
# Post-Sprint-3: the confidence type inconsistency, and customer identity
# --------------------------------------------------------------------------
def test_confidence_accepts_decimal_as_well_as_float():
    """This validator used to reject Decimal while resolver.Assessment rejected
    float — two modules disagreeing about the type of one concept, which blocked
    carrying a probabilistic score into a leak without a lossy conversion."""
    from decimal import Decimal

    assert LeakRecord(id="l1", amount=Money.of("1", "INR"),
                      leak_type=LeakType.SHORT_PAYMENT,
                      confidence=Decimal("0.7")).confidence == Decimal("0.7")
    assert Transaction(id="t1", source=Source.BANK, gross_amount=Money.of("1", "INR"),
                       ts=datetime(2026, 8, 25, 9, 0),
                       match_confidence=Decimal("0.9")).match_confidence == Decimal("0.9")


def test_a_non_finite_decimal_confidence_is_still_rejected():
    from decimal import Decimal

    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), Decimal("1.5")):
        with pytest.raises(DomainError):
            LeakRecord(id="l1", amount=Money.of("1", "INR"),
                       leak_type=LeakType.SHORT_PAYMENT, confidence=bad)


def test_a_bool_is_still_not_a_confidence():
    with pytest.raises(DomainError):
        LeakRecord(id="l1", amount=Money.of("1", "INR"),
                   leak_type=LeakType.SHORT_PAYMENT, confidence=True)


def test_a_leak_can_name_whose_money_it_is():
    l = LeakRecord(id="l1", amount=Money.of("1", "INR"),
                   leak_type=LeakType.SHORT_PAYMENT, customer_ref="cust-1")
    assert l.customer_ref == "cust-1"
    assert LeakRecord(id="l2", amount=Money.of("1", "INR"),
                      leak_type=LeakType.SHORT_PAYMENT).customer_ref is None


def test_an_empty_customer_ref_is_refused():
    """Empty-string identity would silently pool every anonymous leak into one
    'customer' and share a contact allowance between strangers."""
    with pytest.raises(DomainError):
        LeakRecord(id="l1", amount=Money.of("1", "INR"),
                   leak_type=LeakType.SHORT_PAYMENT, customer_ref="")
