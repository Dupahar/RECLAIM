"""Capstone — the full detection pipeline end to end.

    exact gate (Phase 4)  ->  probabilistic review band (Phase 5)
                          ->  gated AI resolver (Phase 6)

Proves the three stages compose: what the exact matcher can't match falls to
the probabilistic scorer, whose review-band candidates are adjudicated by the
gated resolver — which either confirms (consensus + confidence + verified) or
safely escalates to a human. Uses deterministic fake resolvers, so the whole
stack is provable offline.
"""
from datetime import datetime
from decimal import Decimal

from reclaim.money import Money
from reclaim.domain import Source, Transaction, TransactionRefs
from reclaim.reconciliation import reconcile_settlements_to_bank
from reclaim.probabilistic import probabilistic_match
from reclaim.resolver import Decision, GatedResolver, StaticResolver

D = Decimal
TS = datetime(2026, 8, 25, 9, 0, 0)


def txn(tid, source, amount, utr):
    return Transaction(id=tid, source=source, gross_amount=Money.of(amount, "INR"), ts=TS,
                       refs=TransactionRefs(utr=utr))


def _residual_review_candidates():
    settlements = [txn("s1", Source.SETTLEMENT, "4882.12", "U1"),
                   txn("s2", Source.SETTLEMENT, "3000.00", "U2")]
    banks = [txn("b1", Source.BANK, "4882.12", "U1X"),   # garbled UTR
             txn("b2", Source.BANK, "3000.00", "U2")]
    exact = reconcile_settlements_to_bank(settlements, banks)
    fuzzy = probabilistic_match(list(exact.unmatched_settlements),
                                list(exact.unmatched_bank_credits))
    assert exact.matched_count == 1 and fuzzy.review_count == 1
    return fuzzy.review_candidates


def test_pipeline_confirms_a_reviewed_match():
    candidates = _residual_review_candidates()
    resolver = GatedResolver(StaticResolver(True, D("0.9")), StaticResolver(True, D("0.95")))
    outcomes = resolver.resolve_many(candidates)
    assert len(outcomes) == 1
    out = outcomes[0]
    assert out.decision is Decision.CONFIRMED_MATCH
    assert out.candidate.settlement.id == "s1" and out.candidate.bank.id == "b1"


def test_pipeline_escalates_when_model_unsure():
    candidates = _residual_review_candidates()
    resolver = GatedResolver(StaticResolver(True, D("0.5")))  # low confidence
    out = resolver.resolve_many(candidates)[0]
    assert out.decision is Decision.ESCALATE_HUMAN  # never a guess on money
