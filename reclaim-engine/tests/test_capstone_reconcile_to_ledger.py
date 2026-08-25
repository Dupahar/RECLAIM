"""Capstone integration — the whole foundation working as one capability.

Proves the RECLAIM loop's deterministic core end-to-end:
  reconcile (Phase 4) -> post matched settlements to the double-entry ledger
  (Phase 3) using domain types (Phase 2) and exact Money (Phase 1) -> emit the
  honest 3-number summary (match rate / matched amount / leaked residual).

This is the "detect -> post -> prove" arc from the product & architecture docs,
built entirely on the AI-free foundation.
"""
from datetime import datetime

from reclaim.money import Money
from reclaim.domain import Direction, Fees, LedgerEntry, Source, Transaction, TransactionRefs
from reclaim.ledger import Ledger, Posting
from reclaim.reconciliation import MatchPair, reconcile_settlements_to_bank

TS = datetime(2026, 8, 25, 8, 0, 0)


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def posting_for(pair: MatchPair) -> Posting:
    """Turn a matched settlement into a balanced double-entry posting.

    debit bank_account (payout) + debit fee_expense (fees) == credit sales_clearing (gross)
    """
    s = pair.settlement
    entries = [
        LedgerEntry(f"{s.id}:bank", s.id, "bank_account", Direction.DEBIT, s.net_amount, s.ts),
        LedgerEntry(f"{s.id}:sales", s.id, "sales_clearing", Direction.CREDIT, s.gross_amount, s.ts),
    ]
    if s.fees is not None and s.fees.total().is_positive:
        entries.insert(1, LedgerEntry(f"{s.id}:fee", s.id, "fee_expense", Direction.DEBIT,
                                      s.fees.total(), s.ts))
    return Posting(id=s.id, ts=s.ts, entries=tuple(entries))


def test_full_foundation_loop():
    fees = Fees(inr("95.66"), inr("17.22"), inr("5.00"), inr("0"))  # total 117.88
    settlements = [
        Transaction(id="s1", source=Source.SETTLEMENT, gross_amount=inr("5000.00"), ts=TS,
                    fees=fees, refs=TransactionRefs(utr="U1")),          # payout 4882.12 -> match
        Transaction(id="s2", source=Source.SETTLEMENT, gross_amount=inr("3000.00"), ts=TS,
                    refs=TransactionRefs(utr="U2")),                     # payout 3000 -> match
        Transaction(id="s3", source=Source.SETTLEMENT, gross_amount=inr("1000.00"), ts=TS,
                    refs=TransactionRefs(utr="U3")),                     # missing -> leak
    ]
    banks = [
        Transaction(id="b1", source=Source.BANK, gross_amount=inr("4882.12"), ts=TS,
                    refs=TransactionRefs(utr="U1")),
        Transaction(id="b2", source=Source.BANK, gross_amount=inr("3000.00"), ts=TS,
                    refs=TransactionRefs(utr="U2")),
        Transaction(id="b9", source=Source.BANK, gross_amount=inr("500.00"), ts=TS,
                    refs=TransactionRefs(utr="U9")),                     # unexpected -> leak
    ]

    # 1) Reconcile.
    result = reconcile_settlements_to_bank(settlements, banks)
    assert result.matched_count == 2
    assert result.leak_count == 2                      # missing 1000 + unexpected 500
    assert result.total_expected == inr("8882.12")     # 4882.12 + 3000 + 1000
    assert result.matched_amount() == inr("7882.12")   # 4882.12 + 3000
    assert result.leaked_amount() == inr("1500.00")    # 1000 + 500

    # 2) Post matched settlements to the ledger.
    ledger = Ledger()
    for pair in result.matched:
        ledger.post(posting_for(pair))

    # 3) Prove integrity: the ledger balances and reflects the recovered money.
    assert ledger.is_globally_balanced("INR")
    assert ledger.balance("bank_account", "INR") == result.matched_amount()  # 7882.12
    assert ledger.balance("sales_clearing", "INR") == inr("-8000.00")        # credit-normal
    assert ledger.balance("fee_expense", "INR") == inr("117.88")

    # 4) The honest 3-number summary (what a human/dashboard sees).
    summary = {
        "match_rate_by_value": result.match_rate_by_value(),   # 7882.12 / 8882.12
        "matched": str(result.matched_amount()),
        "leaked_residual": str(result.leaked_amount()),
    }
    assert summary["matched"] == "7882.12 INR"
    assert summary["leaked_residual"] == "1500.00 INR"
    # 7882.12 / 8882.12 = 0.88742...  -> 0.8874 at 4dp
    assert str(summary["match_rate_by_value"]) == "0.8874"


def test_posting_is_idempotent_on_reprocess():
    """Re-running reconciliation + posting must not double-count (deterministic ids)."""
    settlements = [Transaction(id="s1", source=Source.SETTLEMENT, gross_amount=inr("100.00"),
                               ts=TS, refs=TransactionRefs(utr="U1"))]
    banks = [Transaction(id="b1", source=Source.BANK, gross_amount=inr("100.00"),
                         ts=TS, refs=TransactionRefs(utr="U1"))]
    ledger = Ledger()
    for _ in range(2):  # process the same batch twice
        result = reconcile_settlements_to_bank(settlements, banks)
        for pair in result.matched:
            ledger.post(posting_for(pair))
    assert len(ledger.postings()) == 1                       # idempotent — no double post
    assert ledger.balance("bank_account", "INR") == inr("100.00")
