"""Capstone — the two-brain reconciliation flow (exact gate -> probabilistic).

Demonstrates the architecture's core design: the exact matcher (Phase 4) is a
strict gate; whatever it cannot match by exact UTR falls through to the
probabilistic layer (Phase 5), which recovers likely matches the exact matcher
missed (here: a garbled UTR) and routes the ambiguous one to the review band
for the future AI/human resolver. Net effect: fewer false leaks, honestly
labelled by confidence.
"""
from datetime import datetime
from decimal import Decimal

from reclaim.money import Money
from reclaim.domain import Source, Transaction, TransactionRefs
from reclaim.reconciliation import reconcile_settlements_to_bank
from reclaim.probabilistic import probabilistic_match


def inr(x: str) -> Money:
    return Money.of(x, "INR")


TS = datetime(2026, 8, 25, 9, 0, 0)


def txn(tid, source, amount, utr):
    return Transaction(id=tid, source=source, gross_amount=inr(amount), ts=TS,
                       refs=TransactionRefs(utr=utr))


def test_probabilistic_recovers_what_exact_missed():
    settlements = [
        txn("s1", Source.SETTLEMENT, "4882.12", "U1"),   # UTR U1
        txn("s2", Source.SETTLEMENT, "3000.00", "U2"),   # clean exact match
    ]
    banks = [
        txn("b1", Source.BANK, "4882.12", "U1X"),        # same money, garbled UTR
        txn("b2", Source.BANK, "3000.00", "U2"),         # clean exact match
    ]

    # Brain 1 — exact gate.
    exact = reconcile_settlements_to_bank(settlements, banks)
    assert exact.matched_count == 1                       # only s2<->b2 by exact UTR
    assert len(exact.unmatched_settlements) == 1          # s1 (no U1 in banks)
    assert len(exact.unmatched_bank_credits) == 1         # b1 (U1X not in settlements)
    # Naively, that's TWO leaks for what is really one payment.

    # Brain 2 — probabilistic on the residual.
    fuzzy = probabilistic_match(list(exact.unmatched_settlements),
                                list(exact.unmatched_bank_credits))
    # amount exact (0.5) + same day (0.2) + no shared ref (0.0) = 0.70 -> review band
    assert fuzzy.auto_count == 0
    assert fuzzy.review_count == 1
    cand = fuzzy.review_candidates[0]
    assert cand.settlement.id == "s1" and cand.bank.id == "b1"
    assert cand.score == Decimal("0.7000")
    assert fuzzy.residual_settlements == () and fuzzy.residual_banks == ()

    # Net honest picture: 1 exact match + 1 high-confidence *candidate* awaiting
    # confirmation, instead of 2 unexplained leaks.
