"""Integration recheck — Phases 0+1+2 working *together*.

Unit tests prove each layer in isolation. This file proves they compose:
a realistic Razorpay-style settlement flows through Money -> Fees ->
Transaction, its double-entry representation balances (debits == credits),
and a failed-debit LeakRecord is well-formed. This is the end-to-end
"foundation is coherent" check before building the ledger (Phase 3).
"""
from datetime import datetime
from decimal import Decimal

from reclaim.money import Money
from reclaim.domain import (
    Direction,
    Fees,
    LeakRecord,
    LeakType,
    LedgerEntry,
    RecoveryState,
    Source,
    Transaction,
    TransactionRefs,
)

TS = datetime(2026, 8, 25, 10, 30, 0)


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def test_settlement_flows_through_all_three_layers():
    """Gross ₹5,00,000 → 2% MDR + 18% GST-on-MDR → payout ₹4,88,200,
    computed with Money math and modeled as a balanced double-entry set."""
    gross = inr("500000.00")

    # Fees computed with Money multiply: precision preserved, then rounded (Phase 1).
    mdr = (gross * Decimal("0.02")).round()          # 10,000.00
    gst_on_mdr = (mdr * Decimal("0.18")).round()     # 1,800.00
    fees = Fees(mdr=mdr, gst_on_mdr=gst_on_mdr, tcs=Money.zero("INR"), other=Money.zero("INR"))

    assert mdr == inr("10000.00")
    assert gst_on_mdr == inr("1800.00")
    assert fees.total() == inr("11800.00")

    # Domain Transaction (Phase 2) composes with Money + Fees.
    settlement = Transaction(
        id="settle-2026-08-25-001",
        source=Source.SETTLEMENT,
        gross_amount=gross,
        ts=TS,
        fees=fees,
        refs=TransactionRefs(utr="UTR8842X"),
        counterparty="Razorpay",
    )
    payout = settlement.net_amount
    assert payout == inr("488200.00")

    # Model the settlement as double-entry LedgerEntry objects (Phase 2 types).
    # Cash received + fee expense (debits) must equal sales clearing (credit).
    entries = [
        LedgerEntry(id="e1", txn_id=settlement.id, account="bank_account",
                    direction=Direction.DEBIT, amount=payout, ts=TS),
        LedgerEntry(id="e2", txn_id=settlement.id, account="fee_expense",
                    direction=Direction.DEBIT, amount=fees.total(), ts=TS),
        LedgerEntry(id="e3", txn_id=settlement.id, account="sales_clearing",
                    direction=Direction.CREDIT, amount=gross, ts=TS),
    ]

    debits = sum((e.amount for e in entries if e.direction is Direction.DEBIT), Money.zero("INR"))
    credits = sum((e.amount for e in entries if e.direction is Direction.CREDIT), Money.zero("INR"))
    assert debits == credits == gross          # the double-entry invariant holds by construction


def test_failed_debit_leak_is_wellformed():
    """A ₹499 failed Autopay debit becomes a recoverable LeakRecord."""
    leak = LeakRecord(
        id="leak-sub-4412",
        amount=inr("499.00"),
        leak_type=LeakType.FAILED_DEBIT,
        source_refs=("mandate-4412", "invoice-9931"),
        hypothesis="temporary insufficient balance; retriable at predicted funded-moment",
        confidence=0.82,
        recoverable=True,
        recovery_state=RecoveryState.PENDING,
    )
    assert leak.amount == inr("499.00")
    assert leak.recoverable and leak.recovery_state is RecoveryState.PENDING


def test_no_float_anywhere_in_the_pipeline():
    """End-to-end exactness: the composed result is exact, unlike float math."""
    gross = inr("0.10") + inr("0.20")          # exact
    assert gross == inr("0.30")
    txn = Transaction(id="t", source=Source.ORDERS, gross_amount=gross, ts=TS)
    assert txn.net_amount.amount == Decimal("0.30")
    # the float world would have produced 0.30000000000000004
    assert (0.1 + 0.2) != 0.3
