"""Phase 4 tests — deterministic reconciliation core."""
from datetime import datetime
from decimal import Decimal

import pytest

from reclaim.money import Money
from reclaim.domain import Fees, LeakType, Source, Transaction, TransactionRefs
from reclaim.reconciliation import (
    ReconciliationError,
    reconcile_settlements_to_bank,
)

TS = datetime(2026, 8, 25, 8, 0, 0)


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def settlement(sid, utr, gross, fees_total="0"):
    fees = Fees(inr(fees_total), inr("0"), inr("0"), inr("0")) if fees_total != "0" else None
    return Transaction(id=sid, source=Source.SETTLEMENT, gross_amount=inr(gross), ts=TS,
                       fees=fees, refs=TransactionRefs(utr=utr))


def bank(bid, utr, amount):
    return Transaction(id=bid, source=Source.BANK, gross_amount=inr(amount), ts=TS,
                       refs=TransactionRefs(utr=utr))


# --------------------------------------------------------------------------
# Happy path & rates
# --------------------------------------------------------------------------
def test_all_match():
    settlements = [settlement("s1", "U1", "90000.00"), settlement("s2", "U2", "8000.00")]
    banks = [bank("b1", "U1", "90000.00"), bank("b2", "U2", "8000.00")]
    res = reconcile_settlements_to_bank(settlements, banks)
    assert res.matched_count == 2
    assert res.leak_count == 0
    assert res.match_rate_by_count() == Decimal("1.0000")
    assert res.match_rate_by_value() == Decimal("1.0000")
    assert res.matched_amount() == inr("98000.00")


def test_missing_settlement_leak_and_rates():
    # 3 settlements totalling 100000; the 2000 one has no bank credit.
    settlements = [
        settlement("s1", "U1", "90000.00"),
        settlement("s2", "U2", "8000.00"),
        settlement("s3", "U3", "2000.00"),
    ]
    banks = [bank("b1", "U1", "90000.00"), bank("b2", "U2", "8000.00")]
    res = reconcile_settlements_to_bank(settlements, banks)
    assert res.matched_count == 2
    assert res.leak_count == 1
    leak = res.leaks[0]
    assert leak.leak_type is LeakType.MISSING_SETTLEMENT
    assert leak.amount == inr("2000.00")
    assert res.total_expected == inr("100000.00")
    # by value: 98000 / 100000 = 0.9800
    assert res.match_rate_by_value() == Decimal("0.9800")
    # by count: 2 / 3 = 0.6667
    assert res.match_rate_by_count() == Decimal("0.6667")


def test_leaked_amount_totals_all_leaks():
    settlements = [
        settlement("s1", "U1", "90000.00"),   # matches
        settlement("s2", "U2", "8000.00"),    # short by 500
        settlement("s3", "U3", "2000.00"),    # missing
    ]
    banks = [bank("b1", "U1", "90000.00"), bank("b2", "U2", "7500.00")]
    res = reconcile_settlements_to_bank(settlements, banks)
    # leaks: short 500 + missing 2000 = 2500
    assert res.leaked_amount() == inr("2500.00")
    assert res.matched_amount() == inr("90000.00")


def test_settlement_decomposition_used_for_matching():
    # gross 5000, fees 117.88 -> expected payout 4882.12; bank credits exactly that.
    fees = Fees(inr("95.66"), inr("17.22"), inr("5.00"), inr("0"))
    s = Transaction(id="s1", source=Source.SETTLEMENT, gross_amount=inr("5000.00"), ts=TS,
                    fees=fees, refs=TransactionRefs(utr="U1"))
    res = reconcile_settlements_to_bank([s], [bank("b1", "U1", "4882.12")])
    assert res.matched_count == 1 and res.leak_count == 0


# --------------------------------------------------------------------------
# Leak types
# --------------------------------------------------------------------------
def test_short_payment_leak_is_recoverable():
    s = settlement("s1", "U1", "1000.00")
    res = reconcile_settlements_to_bank([s], [bank("b1", "U1", "950.00")])
    assert res.matched_count == 0
    leak = res.leaks[0]
    assert leak.leak_type is LeakType.SHORT_PAYMENT
    assert leak.amount == inr("50.00")     # the shortfall
    assert leak.recoverable is True


def test_over_credit_leak():
    s = settlement("s1", "U1", "1000.00")
    res = reconcile_settlements_to_bank([s], [bank("b1", "U1", "1010.00")])
    leak = res.leaks[0]
    assert leak.leak_type is LeakType.UNEXPLAINED_FEE
    assert leak.amount == inr("10.00")     # the excess
    assert leak.recoverable is False


def test_unexpected_bank_credit_leak():
    s = settlement("s1", "U1", "1000.00")
    banks = [bank("b1", "U1", "1000.00"), bank("b2", "U9", "500.00")]  # U9 unmatched
    res = reconcile_settlements_to_bank([s], banks)
    assert res.matched_count == 1
    assert res.leak_count == 1
    leak = res.leaks[0]
    assert leak.leak_type is LeakType.TIMING
    assert leak.amount == inr("500.00")


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------
def test_reconciliation_is_deterministic():
    settlements = [settlement("s1", "U1", "100.00"), settlement("s2", "U2", "200.00")]
    banks = [bank("b1", "U1", "100.00")]
    a = reconcile_settlements_to_bank(settlements, banks)
    b = reconcile_settlements_to_bank(settlements, banks)
    assert a == b  # frozen dataclasses compare structurally -> identical results


# --------------------------------------------------------------------------
# Input validation / error paths
# --------------------------------------------------------------------------
def test_missing_utr_rejected():
    s = Transaction(id="s1", source=Source.SETTLEMENT, gross_amount=inr("1"), ts=TS)  # no utr
    with pytest.raises(ReconciliationError):
        reconcile_settlements_to_bank([s], [])


def test_duplicate_utr_rejected():
    with pytest.raises(ReconciliationError):
        reconcile_settlements_to_bank(
            [settlement("s1", "U1", "1"), settlement("s2", "U1", "1")], []
        )


def test_currency_mix_rejected():
    s = settlement("s1", "U1", "100.00")
    b = Transaction(id="b1", source=Source.BANK, gross_amount=Money.of("100.00", "USD"), ts=TS,
                    refs=TransactionRefs(utr="U1"))
    with pytest.raises(ReconciliationError):
        reconcile_settlements_to_bank([s], [b])


def test_wrong_source_rejected():
    not_a_settlement = bank("x1", "U1", "1.00")  # source=BANK
    with pytest.raises(ReconciliationError):
        reconcile_settlements_to_bank([not_a_settlement], [])
    with pytest.raises(ReconciliationError):
        reconcile_settlements_to_bank([], [settlement("s1", "U1", "1.00")])


def test_empty_input():
    res = reconcile_settlements_to_bank([], [])
    assert res.matched_count == 0 and res.leak_count == 0
    assert res.match_rate_by_count() == Decimal("1.0000")
    assert res.match_rate_by_value() == Decimal("1.0000")
