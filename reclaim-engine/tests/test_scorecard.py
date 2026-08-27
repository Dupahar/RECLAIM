"""Phase 23 tests — the scorecard (architecture §9.3)."""
from decimal import Decimal

from reclaim.money import Money
from reclaim.measurement import Arm, HoldoutPolicy, Observation, measure_lift
from reclaim.scorecard import build_scorecard
from reclaim.demo import TS, build_demo_batch, run_demo
from reclaim.pipeline import run_reclaim
from reclaim.recovery import (
    AlwaysNotifies,
    AlwaysSucceedsExecutor,
    RecoveryEngine,
)
from reclaim.resolver import GatedResolver, StaticResolver

D = Decimal


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def test_scorecard_carries_the_five_parts():
    sc = build_scorecard(run_demo())
    assert sc.match_rate == D("0.6299")
    assert sc.closure_rate == D("0.6496")
    assert sc.gross_recovered == inr("200.00")
    assert sc.residual_amount == inr("750.00") and sc.residual_count == 1
    assert sc.units_contacted == 1 and sc.total_attempts == 1
    assert sc.time_to_closure_hours == D("24.0000")     # notice window, then the debit


def test_gross_recovery_is_not_reported_as_causal_without_a_control():
    """The refusal that makes the scorecard ungameable."""
    sc = build_scorecard(run_demo())
    assert sc.gross_recovered == inr("200.00")
    assert sc.causal_recovered is None
    assert sc.lift_pp is None
    assert "cannot be credited causally" in sc.causal_note


def test_causal_figure_appears_once_a_lift_is_measured():
    observations = ([Observation(f"t{i}", Arm.TREATED, inr("100.00"), i < 60) for i in range(100)]
                    + [Observation(f"c{i}", Arm.CONTROL, inr("100.00"), i < 25) for i in range(100)])
    sc = build_scorecard(run_demo(), measure_lift(observations))
    assert sc.lift_pp == D("35.0000")
    assert sc.causal_recovered == inr("3500.00")


def test_an_unmeasurable_lift_still_blanks_the_causal_figure():
    only_treated = [Observation(f"t{i}", Arm.TREATED, inr("100.00"), True) for i in range(5)]
    sc = build_scorecard(run_demo(), measure_lift(only_treated))
    assert sc.causal_recovered is None and sc.lift_pp is None
    assert "not zero" in sc.causal_note


def test_notice_compliance_is_zero_when_no_notice_was_sent():
    """The demo models the 24h window without serving it — and says so."""
    assert build_scorecard(run_demo()).notice_compliance == D("0.0000")


def test_notice_compliance_is_one_with_a_real_notice_executor():
    settlements, banks = build_demo_batch()
    engine = RecoveryEngine(AlwaysSucceedsExecutor(), notice_executor=AlwaysNotifies())
    report = run_reclaim(settlements, banks,
                         resolver=GatedResolver(StaticResolver(True, D("0.9")),
                                                StaticResolver(True, D("0.95"))),
                         recovery_engine=engine, base_time=TS)
    assert build_scorecard(report).notice_compliance == D("1.0000")


def test_holdout_leaks_are_left_alone_and_counted_in_the_queue():
    settlements, banks = build_demo_batch()
    engine = RecoveryEngine(AlwaysSucceedsExecutor())
    everyone_control = HoldoutPolicy(control_pct=100)
    report = run_reclaim(settlements, banks, recovery_engine=engine, holdout=everyone_control, base_time=TS)
    assert report.recoveries == ()                     # nothing was chased
    assert len(report.control_leaks) == 1              # the short payment was held out
    assert report.recovered_amount == inr("0")
    sc = build_scorecard(report)
    assert sc.units_contacted == 0
    assert sc.contacts_per_unit is None                # no contacts -> no rate, not 0
    assert sc.wasted_contact_rate is None
    assert sc.notice_compliance is None
    assert sc.time_to_closure_hours is None
    assert sc.open_queue_count == len(report.residual_leaks) + 1


def test_holdout_of_zero_percent_treats_everyone():
    settlements, banks = build_demo_batch()
    report = run_reclaim(settlements, banks, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                         holdout=HoldoutPolicy(control_pct=0), base_time=TS)
    assert report.control_leaks == ()
    assert report.recovered_amount == inr("200.00")


def test_summary_is_json_friendly():
    s = build_scorecard(run_demo()).summary()
    assert s["match_rate"] == "0.6299" and s["closure_rate"] == "0.6496"
    assert s["causal_recovered"] is None
    assert all(not isinstance(v, (Money, Decimal)) for v in s.values())
