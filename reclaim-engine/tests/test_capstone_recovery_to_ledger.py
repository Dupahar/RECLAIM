"""Capstone — recover a leak and book the recovered money.

    reconcile (detect short payment) -> recovery engine (win it back)
                                     -> ledger (post the recovered money)

Closes the loop for a recoverable leak: the reconciler finds a shortfall, the
recovery engine reclaims it (bounded + compliant), and the recovered amount is
posted as a balanced double-entry — the "detect -> recover -> book" arc.
"""
from datetime import datetime

from reclaim.money import Money
from reclaim.domain import Direction, LeakType, LedgerEntry, Source, Transaction, TransactionRefs, RecoveryState
from reclaim.reconciliation import reconcile_settlements_to_bank
from reclaim.recovery import AlwaysSucceedsExecutor, FailureReason, RecoveryEngine
from reclaim.ledger import Ledger, Posting

TS = datetime(2026, 8, 25, 9, 0, 0)


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def test_recover_shortfall_and_post_to_ledger():
    # Reconcile: settlement expects 1000, bank credited only 950 -> short payment of 50.
    settlements = [Transaction(id="s1", source=Source.SETTLEMENT, gross_amount=inr("1000.00"),
                               ts=TS, refs=TransactionRefs(utr="U1"))]
    banks = [Transaction(id="b1", source=Source.BANK, gross_amount=inr("950.00"),
                         ts=TS, refs=TransactionRefs(utr="U1"))]
    result = reconcile_settlements_to_bank(settlements, banks)
    assert result.leak_count == 1
    shortfall = result.leaks[0]
    assert shortfall.leak_type is LeakType.SHORT_PAYMENT
    assert shortfall.recoverable and shortfall.amount == inr("50.00")

    # Recover it (temporary cause; fake success).
    outcome = RecoveryEngine(AlwaysSucceedsExecutor()).recover(
        shortfall, FailureReason.INSUFFICIENT_FUNDS, TS)
    assert outcome.final_state is RecoveryState.RECOVERED
    assert outcome.recovered_amount == inr("50.00")

    # Book the recovered money as a balanced posting.
    recovered = outcome.recovered_amount
    ledger = Ledger()
    ledger.post(Posting(id=f"recovery:{shortfall.id}", ts=TS, entries=(
        LedgerEntry(id=f"{shortfall.id}:r-bank", txn_id=f"recovery:{shortfall.id}",
                    account="bank_account", direction=Direction.DEBIT, amount=recovered, ts=TS),
        LedgerEntry(id=f"{shortfall.id}:r-recv", txn_id=f"recovery:{shortfall.id}",
                    account="merchant_receivable", direction=Direction.CREDIT, amount=recovered, ts=TS),
    )))

    assert ledger.is_globally_balanced("INR")
    assert ledger.balance("bank_account", "INR") == inr("50.00")
