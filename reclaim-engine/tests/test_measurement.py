"""Phase 22 tests — causal measurement (architecture §9.2, goal G9)."""
from decimal import Decimal

import pytest

from reclaim.money import Money
from reclaim.measurement import (
    Arm,
    CausalLift,
    HoldoutPolicy,
    MeasurementError,
    Observation,
    measure_lift,
)

D = Decimal


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def obs(uid, arm, recovered, amount="100.00"):
    return Observation(unit_id=uid, arm=arm, amount=inr(amount), recovered=recovered)


def cohort(prefix, arm, n, recovered_n, amount="100.00"):
    return [obs(f"{prefix}{i}", arm, i < recovered_n, amount) for i in range(n)]


# --------------------------------------------------------------------------
# Deterministic holdout
# --------------------------------------------------------------------------
def test_assignment_is_stable_and_reproducible():
    p = HoldoutPolicy(control_pct=20)
    assert p.arm("leak:7") == p.arm("leak:7")
    assert HoldoutPolicy(control_pct=20).arm("leak:7") == p.arm("leak:7")


def test_assignment_is_roughly_uniform():
    p = HoldoutPolicy(control_pct=20)
    control = sum(1 for i in range(4000) if p.arm(f"leak:{i}") is Arm.CONTROL)
    assert 700 < control < 900          # 20% of 4000 = 800, generous band


def test_salt_defines_independent_experiments():
    a = HoldoutPolicy(control_pct=50, salt="exp-a")
    b = HoldoutPolicy(control_pct=50, salt="exp-b")
    differing = sum(1 for i in range(200) if a.arm(f"u{i}") != b.arm(f"u{i}"))
    assert differing > 50               # cohorts are not merely copied across experiments


def test_extreme_holdout_percentages():
    assert all(HoldoutPolicy(0).arm(f"u{i}") is Arm.TREATED for i in range(50))
    assert all(HoldoutPolicy(100).arm(f"u{i}") is Arm.CONTROL for i in range(50))


def test_holdout_validation():
    for bad in (-1, 101, "10", True):
        with pytest.raises(MeasurementError):
            HoldoutPolicy(control_pct=bad)
    with pytest.raises(MeasurementError):
        HoldoutPolicy(salt="")
    with pytest.raises(MeasurementError):
        HoldoutPolicy().arm("")


def test_observation_validation():
    with pytest.raises(MeasurementError):
        Observation("", Arm.TREATED, inr("1"), True)
    with pytest.raises(MeasurementError):
        Observation("u", "treated", inr("1"), True)
    with pytest.raises(MeasurementError):
        Observation("u", Arm.TREATED, "1", True)
    with pytest.raises(MeasurementError):
        Observation("u", Arm.TREATED, inr("1"), "yes")


# --------------------------------------------------------------------------
# The measurement itself
# --------------------------------------------------------------------------
def test_lift_subtracts_what_would_have_happened_anyway():
    """The core claim: gross recovery is not the value the product added."""
    data = cohort("t", Arm.TREATED, 100, 60) + cohort("c", Arm.CONTROL, 100, 25)
    lift = measure_lift(data)
    assert lift.treated.rate() == D("0.6000")
    assert lift.control.rate() == D("0.2500")
    assert lift.lift_pp == D("35.0000")
    assert lift.treated.recovered_amount == inr("6000.00")   # gross
    assert lift.incremental_amount == inr("3500.00")         # causal
    assert lift.is_measurable() and not lift.underpowered


def test_a_worthless_intervention_measures_as_worthless():
    data = cohort("t", Arm.TREATED, 100, 40) + cohort("c", Arm.CONTROL, 100, 40)
    lift = measure_lift(data)
    assert lift.lift_pp == D("0.0000")
    assert lift.incremental_amount == inr("0.00")            # gross was 4000


def test_a_harmful_intervention_reports_negative_lift():
    data = cohort("t", Arm.TREATED, 100, 20) + cohort("c", Arm.CONTROL, 100, 50)
    lift = measure_lift(data)
    assert lift.lift_pp == D("-30.0000")
    assert lift.incremental_amount.is_negative


def test_no_control_group_reports_none_not_a_perfect_score():
    """The failure mode this module exists to prevent."""
    lift = measure_lift(cohort("t", Arm.TREATED, 50, 30))
    assert lift.lift_pp is None
    assert lift.incremental_amount is None
    assert not lift.is_measurable()
    assert lift.underpowered
    assert "not zero" in lift.note
    assert lift.treated.recovered_amount == inr("3000.00")   # gross is still reported


def test_no_treated_group_is_also_undefined():
    lift = measure_lift(cohort("c", Arm.CONTROL, 50, 10))
    assert lift.lift_pp is None and "no treated" in lift.note


def test_small_cohorts_are_flagged_underpowered():
    data = cohort("t", Arm.TREATED, 5, 4) + cohort("c", Arm.CONTROL, 5, 1)
    lift = measure_lift(data)
    assert lift.is_measurable()
    assert lift.underpowered
    assert "UNDERPOWERED" in lift.note
    assert measure_lift(data, min_cohort=2).underpowered is False


def test_control_with_zero_value_at_stake():
    data = cohort("t", Arm.TREATED, 40, 20) + cohort("c", Arm.CONTROL, 40, 0, amount="0")
    lift = measure_lift(data)
    assert lift.incremental_amount == lift.treated.recovered_amount
    assert "zero value at stake" in lift.note


def test_rejects_empty_and_mixed_currency_input():
    with pytest.raises(MeasurementError):
        measure_lift([])
    mixed = [obs("a", Arm.TREATED, True),
             Observation("b", Arm.CONTROL, Money.of("1.00", "USD"), False)]
    with pytest.raises(MeasurementError, match="multiple currencies"):
        measure_lift(mixed)


def test_summary_is_json_friendly():
    data = cohort("t", Arm.TREATED, 40, 20) + cohort("c", Arm.CONTROL, 40, 10)
    s = measure_lift(data).summary()
    assert s["treated_n"] == 40 and s["control_n"] == 40
    assert s["lift_pp"] == "25.0000"
    assert s["gross_recovered"] == "2000.00 INR"
    assert s["incremental_recovered"] == "1000.00 INR"
    assert all(not isinstance(v, (Money, Decimal)) for v in s.values())


def test_empty_cohort_rates_are_none():
    lift = measure_lift(cohort("t", Arm.TREATED, 3, 1))
    assert lift.control.rate() is None
    assert lift.control.amount_rate() is None
