"""Phase 8 tests — the end-to-end orchestration pipeline."""
from datetime import datetime
from decimal import Decimal

import pytest

from reclaim.money import Money
from reclaim.domain import Fees, RecoveryState, Source, Transaction, TransactionRefs
from reclaim.pipeline import PipelineError, Targeting, run_reclaim
from reclaim.probabilistic import ScoredMatch  # noqa: F401 (documents the type in report)
from reclaim.resolver import GatedResolver, StaticResolver
from reclaim.recovery import (
    AlwaysFailsExecutor,
    AlwaysSucceedsExecutor,
    FailureReason,
    RecoveryEngine,
)

D = Decimal
TS = datetime(2026, 8, 25, 9, 0, 0)


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def s(sid, gross, utr, fees_total=None):
    fees = None
    if fees_total is not None:
        fees = Fees(inr(fees_total), inr("0"), inr("0"), inr("0"))
    return Transaction(id=sid, source=Source.SETTLEMENT, gross_amount=inr(gross), ts=TS,
                       fees=fees, refs=TransactionRefs(utr=utr))


def bnk(bid, amount, utr):
    return Transaction(id=bid, source=Source.BANK, gross_amount=inr(amount), ts=TS,
                       refs=TransactionRefs(utr=utr))


def _short_pay_batch():
    """One settlement short-paid by 200 -- a single recoverable leak to target."""
    return [s("s2", "3000.00", "U2")], [bnk("b2", "2800.00", "U2")]


# --------------------------------------------------------------------------
# Simplest run — all exact matches
# --------------------------------------------------------------------------
def test_all_exact_matches():
    rep = run_reclaim([s("s1", "1000.00", "U1")], [bnk("b1", "1000.00", "U1")])
    assert rep.match_rate() == D("1.0000")
    assert rep.matched_amount == inr("1000.00")
    assert rep.residual_leaks == ()
    assert rep.ledger.is_globally_balanced("INR")
    assert rep.ledger.balance("bank_account", "INR") == inr("1000.00")


# --------------------------------------------------------------------------
# Full loop — exact + probabilistic(review->AI confirm) + recovery + residual
# --------------------------------------------------------------------------
def test_full_loop_report():
    settlements = [
        s("s1", "1000.00", "U1"),               # exact match
        s("s2", "5000.00", "U2"),               # short payment -> recover
        s("s3", "2000.00", "U3"),               # garbled UTR -> fuzzy review -> AI confirm
        s("s4", "750.00", "U4"),                # truly missing -> residual
    ]
    banks = [
        bnk("b1", "1000.00", "U1"),
        bnk("b2", "4800.00", "U2"),             # short by 200
        bnk("b3", "2000.00", "U3X"),            # garbled UTR, same amount+date
    ]
    resolver = GatedResolver(StaticResolver(True, D("0.9")), StaticResolver(True, D("0.95")))
    engine = RecoveryEngine(AlwaysSucceedsExecutor())
    rep = run_reclaim(settlements, banks, resolver=resolver, recovery_engine=engine, base_time=TS)

    # s1 (exact) + s3 (AI-confirmed) are reconciled -> 1000 + 2000 = 3000
    assert rep.matched_amount == inr("3000.00")
    assert rep.ai_confirmed_count == 1
    # s2 short payment of 200 recovered
    assert rep.recovered_amount == inr("200.00")
    assert any(r.final_state is RecoveryState.RECOVERED for r in rep.recoveries)
    # s4 remains a residual leak (missing, not fuzzy-matched)
    assert len(rep.residual_leaks) == 1
    assert rep.residual_leaks[0].source_refs[0] == "s4"
    # ledger balances after all postings
    assert rep.ledger.is_globally_balanced("INR")
    # total expected = 1000 + 4800(net of s2) ... net_amounts: s1 1000, s2 5000, s3 2000, s4 750
    assert rep.total_expected == inr("8750.00")

    summ = rep.summary()
    assert summ["matched"] == "3000.00 INR"
    assert summ["recovered"] == "200.00 INR"
    assert summ["ai_confirmed"] == 1


# --------------------------------------------------------------------------
# Optional components
# --------------------------------------------------------------------------
def test_no_resolver_leaves_review_pending():
    # garbled UTR pair -> fuzzy review band; no resolver -> pending_review, residual stays
    settlements = [s("s1", "2000.00", "U1")]
    banks = [bnk("b1", "2000.00", "U1X")]
    rep = run_reclaim(settlements, banks)  # no resolver, no recovery
    assert len(rep.pending_review) == 1
    assert rep.ai_outcomes == ()
    assert len(rep.residual_leaks) == 2  # missing s1 + unexpected b1, neither resolved


def test_no_recovery_engine_leaves_shortfall_leak():
    settlements = [s("s1", "1000.00", "U1")]
    banks = [bnk("b1", "900.00", "U1")]         # short by 100
    rep = run_reclaim(settlements, banks)       # no recovery engine
    assert rep.recovered_amount == inr("0")
    assert len(rep.residual_leaks) == 1          # the short payment remains
    assert rep.recoveries == ()


def test_recovery_engine_requires_base_time():
    engine = RecoveryEngine(AlwaysSucceedsExecutor())
    with pytest.raises(PipelineError):
        run_reclaim([s("s1", "1000.00", "U1")], [bnk("b1", "900.00", "U1")], recovery_engine=engine)


def test_ai_escalation_keeps_review_pending_and_residual():
    # fuzzy review candidate, but resolver is unsure -> escalate -> not matched.
    settlements = [s("s1", "2000.00", "U1")]
    banks = [bnk("b1", "2000.00", "U1X")]
    resolver = GatedResolver(StaticResolver(True, D("0.5")))  # low confidence -> escalate
    rep = run_reclaim(settlements, banks, resolver=resolver)
    assert rep.ai_confirmed_count == 0
    assert rep.ai_escalated_count == 1
    assert rep.matched_amount == inr("0")
    assert len(rep.residual_leaks) == 2


# --------------------------------------------------------------------------
# Determinism & empty
# --------------------------------------------------------------------------
def test_determinism():
    settlements = [s("s1", "1000.00", "U1"), s("s2", "2000.00", "U2")]
    banks = [bnk("b1", "1000.00", "U1")]
    a = run_reclaim(settlements, banks).summary()
    b = run_reclaim(settlements, banks).summary()
    assert a == b


def test_empty_batch():
    rep = run_reclaim([], [])
    assert rep.match_rate() == D("1.0000")
    assert rep.residual_leaks == ()
    assert rep.summary()["matched"] == "0 INR"


# --------------------------------------------------------------------------
# Branch coverage: fees posting, probabilistic auto-match, failed recovery
# --------------------------------------------------------------------------
def test_reconciled_settlement_with_fees_posts_fee_entry():
    # gross 1000, fees 100 -> payout 900; bank credits 900 -> exact match.
    settlement = s("s1", "1000.00", "U1", fees_total="100.00")
    rep = run_reclaim([settlement], [bnk("b1", "900.00", "U1")])
    assert rep.ledger.is_globally_balanced("INR")
    assert rep.ledger.balance("fee_expense", "INR") == inr("100.00")
    assert rep.matched_amount == inr("900.00")


def test_probabilistic_auto_match_via_shared_order_id():
    # Different UTRs (exact misses) but a shared order_id -> fuzzy score 1.0 -> AUTO.
    settlement = Transaction(id="s1", source=Source.SETTLEMENT, gross_amount=inr("500.00"),
                             ts=TS, refs=TransactionRefs(utr="U1", order_id="O1"))
    bank = Transaction(id="b1", source=Source.BANK, gross_amount=inr("500.00"),
                       ts=TS, refs=TransactionRefs(utr="U1X", order_id="O1"))
    rep = run_reclaim([settlement], [bank])       # no resolver needed for auto band
    assert len(rep.auto_matched) == 1
    assert rep.matched_amount == inr("500.00")
    assert rep.residual_leaks == ()               # missing + unexpected both resolved by auto


def test_failing_recovery_leaves_leak():
    settlements = [s("s1", "1000.00", "U1")]
    banks = [bnk("b1", "900.00", "U1")]           # short by 100
    engine = RecoveryEngine(AlwaysFailsExecutor())
    rep = run_reclaim(settlements, banks, recovery_engine=engine, base_time=TS)
    assert len(rep.recoveries) == 1
    assert rep.recoveries[0].final_state is RecoveryState.EXHAUSTED
    assert rep.recovered_amount == inr("0")
    assert len(rep.residual_leaks) == 1           # short payment not recovered -> stays


# --------------------------------------------------------------------------
# Phase 26 — uplift targeting inside the loop
# --------------------------------------------------------------------------
def _uplift_bits():
    from reclaim import uplift as U
    return U


def _targeting(treated, control, *, unknown=None, days=1):
    """A Targeting whose model was fitted on one cell with the given counts."""
    U = _uplift_bits()
    from reclaim.measurement import Arm as _Arm

    def context_for(leak):
        return U.Context(failure_reason=FailureReason.INSUFFICIENT_FUNDS,
                         amount=leak.amount, days_since_failure=days, prior_failures=0)

    rows = []
    for arm, (n, got) in ((_Arm.TREATED, treated), (_Arm.CONTROL, control)):
        for i in range(n):
            rows.append(U.TrainingRow(
                context=U.Context(FailureReason.INSUFFICIENT_FUNDS,
                                  Money.of("200.00", "INR"), days, 0),
                arm=arm, recovered=i < got))
    model = U.fit(rows, min_support=10)
    kwargs = {} if unknown is None else {"unknown": unknown}
    return Targeting(model=model, context_for=context_for, **kwargs)


def test_targeting_chases_a_persuadable_leak():
    settlements, banks = _short_pay_batch()
    rep = run_reclaim(settlements, banks, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                      base_time=TS, targeting=_targeting((100, 60), (100, 20)))
    assert rep.skipped_leaks == ()
    assert rep.recovered_amount == Money.of("200.00", "INR")


def test_targeting_skips_a_sure_thing_and_reports_the_value_it_left():
    """Skipping is a decision with a cost, and the cost is in the summary."""
    settlements, banks = _short_pay_batch()
    rep = run_reclaim(settlements, banks, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                      base_time=TS, targeting=_targeting((100, 90), (100, 85)))
    assert rep.recoveries == ()
    assert len(rep.skipped_leaks) == 1
    leak, reason = rep.skipped_leaks[0]
    assert leak.id == "leak:short:s2" and "sure_thing" in reason
    assert rep.skipped_amount() == Money.of("200.00", "INR")
    s = rep.summary()
    assert s["targeting_skipped"] == 1
    assert s["targeting_skipped_amount"] == "200.00 INR"
    # a skipped leak is still an honest residual -- it did not disappear
    assert "leak:short:s2" in {l.id for l in rep.residual_leaks}


def test_targeting_never_chases_a_sleeping_dog():
    settlements, banks = _short_pay_batch()
    rep = run_reclaim(settlements, banks, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                      base_time=TS, targeting=_targeting((100, 30), (100, 60)))
    assert rep.recoveries == ()
    assert "sleeping_dog" in rep.skipped_leaks[0][1]


def test_an_unknown_estimate_still_chases_by_default():
    settlements, banks = _short_pay_batch()
    rep = run_reclaim(settlements, banks, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                      base_time=TS, targeting=_targeting((2, 1), (2, 0)))
    assert rep.skipped_leaks == () and len(rep.recoveries) == 1


def test_the_unknown_policy_can_be_told_to_skip():
    U = _uplift_bits()
    settlements, banks = _short_pay_batch()
    rep = run_reclaim(settlements, banks, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                      base_time=TS,
                      targeting=_targeting((2, 1), (2, 0), unknown=U.UnknownPolicy.SKIP))
    assert len(rep.skipped_leaks) == 1


def test_the_control_arm_is_decided_before_targeting():
    """A held-out unit that targeting would also have skipped must still count
    as control, or the experiment stops measuring the policy that was deployed."""
    from reclaim.measurement import HoldoutPolicy
    settlements, banks = _short_pay_batch()
    rep = run_reclaim(settlements, banks, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                      base_time=TS, holdout=HoldoutPolicy(control_pct=100),
                      targeting=_targeting((100, 90), (100, 85)))     # would skip it
    assert len(rep.control_leaks) == 1
    assert rep.skipped_leaks == ()


def test_targeting_validates_its_parts():
    U = _uplift_bits()
    model = _targeting((10, 6), (10, 2)).model
    with pytest.raises(PipelineError):
        Targeting(model="not a model", context_for=lambda l: None)
    with pytest.raises(PipelineError):
        Targeting(model=model, context_for="not callable")
    with pytest.raises(PipelineError):
        Targeting(model=model, context_for=lambda l: None, unknown="chase")


def test_without_targeting_nothing_changes():
    """Regression guard: the parameter must be invisible when unused."""
    settlements, banks = _short_pay_batch()
    rep = run_reclaim(settlements, banks, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                      base_time=TS)
    assert rep.skipped_leaks == () and rep.skipped_amount() == Money.zero("INR")
    assert rep.summary()["targeting_skipped"] == 0
