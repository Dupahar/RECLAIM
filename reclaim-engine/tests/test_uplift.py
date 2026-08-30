"""Phase 26 tests — uplift modelling (architecture §7).

The reason this module exists is that a response model cannot tell a persuadable
customer from a sure thing. So the tests that matter are the four segment
recoveries — especially the sleeping dog, where chasing destroys value while
looking like activity — plus every refusal to guess on thin evidence.
"""
from decimal import Decimal

import pytest

from reclaim.measurement import Arm
from reclaim.money import Money
from reclaim.recovery import FailureReason
from reclaim.uplift import (
    BASIS_CELL,
    BASIS_GLOBAL,
    BASIS_NONE,
    BASIS_POOLED,
    CellSpec,
    CellStats,
    Context,
    DEFAULT_SPEC,
    Segment,
    SegmentThresholds,
    Selection,
    TrainingRow,
    UnknownPolicy,
    UpliftError,
    UpliftModel,
    decide,
    fit,
    select,
)

D = Decimal


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def ctx(reason=FailureReason.INSUFFICIENT_FUNDS, amount="200.00", days=1, priors=0) -> Context:
    return Context(failure_reason=reason, amount=inr(amount),
                   days_since_failure=days, prior_failures=priors)


def rows(context, *, treated=(0, 0), control=(0, 0)):
    """``treated=(n, recovered)`` — n rows of which `recovered` succeeded."""
    out = []
    for arm, (n, got) in ((Arm.TREATED, treated), (Arm.CONTROL, control)):
        for i in range(n):
            out.append(TrainingRow(context=context, arm=arm, recovered=i < got))
    return out


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def test_context_validates_its_features():
    with pytest.raises(UpliftError):
        Context("insufficient_funds", inr("1"), 0, 0)
    with pytest.raises(UpliftError):
        Context(FailureReason.SOFT_DECLINE, "200", 0, 0)
    with pytest.raises(UpliftError):
        ctx(days=-1)
    with pytest.raises(UpliftError):
        ctx(priors=True)


def test_cell_spec_validates_its_bands():
    with pytest.raises(UpliftError):
        CellSpec(amount_edges=[inr("500")])
    with pytest.raises(UpliftError):
        CellSpec(amount_edges=(500,))
    with pytest.raises(UpliftError):
        CellSpec(amount_edges=(inr("500"), Money.of("100", "USD")))
    with pytest.raises(UpliftError):
        CellSpec(amount_edges=(inr("5000"), inr("500")))          # descending
    with pytest.raises(UpliftError):
        CellSpec(day_edges=())
    with pytest.raises(UpliftError):
        CellSpec(day_edges=(1, "8"))
    with pytest.raises(UpliftError):
        CellSpec(day_edges=(8, 2))
    with pytest.raises(UpliftError):
        CellSpec(prior_failure_edges=(3, 1))


def test_training_row_validates_its_inputs():
    with pytest.raises(UpliftError):
        TrainingRow(context="x", arm=Arm.TREATED, recovered=True)
    with pytest.raises(UpliftError):
        TrainingRow(context=ctx(), arm="treated", recovered=True)
    with pytest.raises(UpliftError):
        TrainingRow(context=ctx(), arm=Arm.TREATED, recovered=1)


def test_thresholds_are_validated_as_a_business_choice():
    with pytest.raises(UpliftError):
        SegmentThresholds(persuadable_uplift=0.05)
    with pytest.raises(UpliftError):
        SegmentThresholds(sleeping_dog_uplift=D("0.05"))          # must be negative
    with pytest.raises(UpliftError):
        SegmentThresholds(persuadable_uplift=D("-0.01"))          # must be positive
    with pytest.raises(UpliftError):
        SegmentThresholds(sure_thing_control_rate=D("1.5"))
    with pytest.raises(UpliftError):
        SegmentThresholds(lost_cause_treated_rate=D("-0.1"))


def test_fit_validates_its_arguments():
    with pytest.raises(UpliftError):
        fit([], spec="nope")
    with pytest.raises(UpliftError):
        fit([], min_support=0)
    with pytest.raises(UpliftError):
        fit([])                                                    # no observations
    with pytest.raises(UpliftError):
        fit(["not a row"])


def test_predict_requires_a_context():
    model = fit(rows(ctx(), treated=(10, 5), control=(10, 1)), min_support=1)
    with pytest.raises(UpliftError):
        model.predict("s1")


def test_a_context_in_the_wrong_currency_is_refused():
    model = fit(rows(ctx(), treated=(10, 5), control=(10, 1)), min_support=1)
    foreign = Context(FailureReason.INSUFFICIENT_FUNDS, Money.of("200", "USD"), 1, 0)
    with pytest.raises(UpliftError) as exc:
        model.predict(foreign)
    assert "bands are INR" in str(exc.value)


# --------------------------------------------------------------------------
# Banding
# --------------------------------------------------------------------------
def test_amount_bands_are_exclusive_upper_bounds():
    spec = DEFAULT_SPEC
    assert spec.amount_band(inr("499.99")) == 0
    assert spec.amount_band(inr("500.00")) == 1          # edge lands in the next band
    assert spec.amount_band(inr("4999.99")) == 1
    assert spec.amount_band(inr("50000.00")) == 2        # above the last edge


def test_a_spec_with_no_amount_edges_uses_one_band():
    spec = CellSpec()
    assert spec.currency is None
    assert spec.amount_band(Money.of("1", "USD")) == 0   # currency-agnostic
    assert spec.amount_band(inr("999999")) == 0


def test_cells_are_stable_and_printable():
    spec = DEFAULT_SPEC
    key = spec.cell(ctx(amount="1000.00", days=5, priors=2))
    assert key == ("insufficient_funds", 1, 1, 1)
    assert spec.cell(ctx(amount="1000.00", days=5, priors=2)) == key


# --------------------------------------------------------------------------
# CellStats
# --------------------------------------------------------------------------
def test_rates_are_none_for_an_empty_arm():
    s = CellStats(treated_n=2, treated_recovered=1)
    assert s.treated_rate == D("0.5000")
    assert s.control_rate is None
    assert CellStats(control_n=2, control_recovered=1).treated_rate is None


def test_support_requires_both_arms():
    """An uplift needs a difference; one arm is not a difference."""
    assert CellStats(10, 5, 10, 1).has_support(10) is True
    assert CellStats(10, 5, 0, 0).has_support(10) is False
    assert CellStats(0, 0, 10, 1).has_support(10) is False
    assert CellStats(9, 5, 10, 1).has_support(10) is False


def test_stats_accumulate_per_arm():
    s = (CellStats().plus(Arm.TREATED, True).plus(Arm.TREATED, False)
         .plus(Arm.CONTROL, False))
    assert (s.treated_n, s.treated_recovered) == (2, 1)
    assert (s.control_n, s.control_recovered) == (1, 0)


# --------------------------------------------------------------------------
# The four quadrants — the reason the module exists
# --------------------------------------------------------------------------
def test_a_persuadable_segment_is_found_and_chased():
    c = ctx()
    model = fit(rows(c, treated=(100, 60), control=(100, 20)), min_support=10)
    est = model.predict(c)
    assert est.segment is Segment.PERSUADABLE
    assert est.uplift == D("0.4000") and est.worth_chasing is True
    assert est.basis == BASIS_CELL


def test_a_sure_thing_is_not_chased_despite_a_high_treated_rate():
    """90% recover when chased — and 85% recover when left alone. A response
    model would rank this top; uplift correctly says do not bother."""
    c = ctx()
    model = fit(rows(c, treated=(100, 90), control=(100, 85)), min_support=10)
    est = model.predict(c)
    assert est.segment is Segment.SURE_THING
    assert est.worth_chasing is False
    assert est.treated_rate == D("0.9000")


def test_a_lost_cause_is_not_chased():
    c = ctx()
    model = fit(rows(c, treated=(100, 5), control=(100, 4)), min_support=10)
    assert model.segment(c) is Segment.LOST_CAUSE


def test_a_sleeping_dog_is_never_chased():
    """Chasing this segment makes the outcome worse. It must be classified as
    harmful rather than merely unprofitable, whatever the other rates say."""
    c = ctx()
    model = fit(rows(c, treated=(100, 30), control=(100, 60)), min_support=10)
    est = model.predict(c)
    assert est.segment is Segment.SLEEPING_DOG
    assert est.uplift == D("-0.3000") and est.worth_chasing is False


def test_a_sleeping_dog_outranks_a_sure_thing_label():
    """Both conditions hold (high control rate *and* negative uplift). Harm
    must win the classification."""
    c = ctx()
    model = fit(rows(c, treated=(100, 60), control=(100, 90)), min_support=10)
    assert model.segment(c) is Segment.SLEEPING_DOG


def test_a_real_but_tiny_uplift_is_not_rounded_up_to_persuadable():
    c = ctx()
    model = fit(rows(c, treated=(100, 42), control=(100, 40)), min_support=10)
    est = model.predict(c)
    assert est.uplift == D("0.0200")
    assert est.segment is Segment.LOST_CAUSE     # positive, but not worth a contact


def test_thresholds_are_configurable():
    c = ctx()
    lenient = SegmentThresholds(persuadable_uplift=D("0.01"),
                                lost_cause_treated_rate=D("0.05"))
    model = fit(rows(c, treated=(100, 42), control=(100, 40)),
                min_support=10, thresholds=lenient)
    assert model.segment(c) is Segment.PERSUADABLE


# --------------------------------------------------------------------------
# The hierarchical fallback — low volume is the normal case
# --------------------------------------------------------------------------
def test_a_thin_cell_falls_back_to_the_failure_reason_pool():
    """The architecture's own open question: can this be estimated for a small
    merchant? Answer: pool, and say that you pooled."""
    thin = ctx(amount="200.00", days=1)
    fat = ctx(amount="200.00", days=20)          # same reason, different day band
    model = fit(rows(thin, treated=(2, 2), control=(2, 0))
                + rows(fat, treated=(100, 60), control=(100, 20)), min_support=10)
    est = model.predict(thin)
    assert est.basis == BASIS_POOLED
    assert est.segment is Segment.PERSUADABLE
    assert est.stats.treated_n == 102            # the pooled counts, not the cell's


def test_a_thin_reason_falls_back_to_the_global_pool():
    rare = ctx(reason=FailureReason.BANK_DOWNTIME)
    common = ctx(reason=FailureReason.INSUFFICIENT_FUNDS)
    model = fit(rows(rare, treated=(1, 1), control=(1, 0))
                + rows(common, treated=(100, 60), control=(100, 20)), min_support=10)
    est = model.predict(rare)
    assert est.basis == BASIS_GLOBAL
    assert est.stats.treated_n == 101


def test_with_no_evidence_the_model_says_so():
    """It does not fall back to a number. INSUFFICIENT_EVIDENCE is the answer."""
    c = ctx()
    model = fit(rows(c, treated=(2, 1), control=(2, 0)), min_support=10)
    est = model.predict(c)
    assert est.segment is Segment.INSUFFICIENT_EVIDENCE
    assert est.uplift is None and est.treated_rate is None
    assert est.basis == BASIS_NONE and est.is_known is False
    assert "no reliable estimate" in est.explain()


def test_a_one_armed_cell_never_produces_an_uplift():
    """Training with no control group at all must not yield a confident model."""
    c = ctx()
    model = fit(rows(c, treated=(500, 300)), min_support=10)
    assert model.segment(c) is Segment.INSUFFICIENT_EVIDENCE


# --------------------------------------------------------------------------
# Model introspection and determinism
# --------------------------------------------------------------------------
def test_the_model_is_explainable_down_to_the_counts():
    c = ctx()
    model = fit(rows(c, treated=(100, 60), control=(100, 20)), min_support=10)
    text = model.predict(c).explain()
    assert "persuadable" in text and "treated n=100" in text and BASIS_CELL in text
    s = model.predict(c).summary()
    assert s["uplift"] == "0.4000" and s["cell"] == ["insufficient_funds", 0, 0, 0]


def test_fitting_is_order_independent_and_deterministic():
    c1, c2 = ctx(), ctx(days=20)
    data = rows(c1, treated=(10, 6), control=(10, 2)) + rows(c2, treated=(10, 1), control=(10, 1))
    a = fit(data, min_support=1)
    b = fit(list(reversed(data)), min_support=1)
    assert a.predict(c1).summary() == b.predict(c1).summary()
    assert a.predict(c2).summary() == b.predict(c2).summary()


def test_model_exposes_its_spec_and_support_threshold():
    model = fit(rows(ctx(), treated=(10, 5), control=(10, 1)), min_support=7)
    assert model.min_support == 7
    assert model.spec is DEFAULT_SPEC
    assert model.cell_count == 1
    assert model.stats_for(ctx()).treated_n == 10
    assert model.stats_for(ctx(days=20)) == CellStats()


def test_a_model_can_be_constructed_directly_from_stats():
    """Useful for a model shipped as a table rather than fitted in-process."""
    key = DEFAULT_SPEC.cell(ctx())
    model = UpliftModel({key: CellStats(50, 30, 50, 10)}, {}, CellStats(), min_support=10)
    assert model.segment(ctx()) is Segment.PERSUADABLE


# --------------------------------------------------------------------------
# Policy — the model proposes, the caller decides
# --------------------------------------------------------------------------
def test_the_unknown_policy_defaults_to_chasing():
    """A model quietly shrinking coverage on thin evidence loses a merchant
    money they were owed. Preserving today's behaviour is the safe default."""
    c = ctx()
    model = fit(rows(c, treated=(2, 1), control=(2, 0)), min_support=10)
    sel = decide(model, c)
    assert sel.chase is True and "chase policy" in sel.reason


def test_the_unknown_policy_can_be_set_to_skip():
    c = ctx()
    model = fit(rows(c, treated=(2, 1), control=(2, 0)), min_support=10)
    sel = decide(model, c, unknown=UnknownPolicy.SKIP)
    assert sel.chase is False and "skipping" in sel.reason


def test_an_invalid_unknown_policy_is_refused():
    model = fit(rows(ctx(), treated=(10, 5), control=(10, 1)), min_support=1)
    with pytest.raises(UpliftError):
        decide(model, ctx(), unknown="chase")


def test_select_targets_a_batch_and_reports_what_it_gave_up():
    """Skipping is not free. The value not chased is part of the output."""
    persuadable = ctx(days=1)
    sure_thing = ctx(days=20, amount="750.00")
    model = fit(rows(persuadable, treated=(100, 60), control=(100, 20))
                + rows(sure_thing, treated=(100, 90), control=(100, 85)), min_support=10)
    out = select(model, {"leak:a": persuadable, "leak:b": sure_thing,
                         "leak:c": sure_thing})
    assert out.chased == 1 and out.skipped == 2
    assert out.skipped_value == inr("1500.00")
    assert out.by_segment == {"persuadable": 1, "sure_thing": 2}
    assert out.summary()["skipped_value"] == "1500.00 INR"
    assert out.selections["leak:a"].chase is True


def test_selecting_nothing_reports_no_forgone_value():
    model = fit(rows(ctx(), treated=(10, 6), control=(10, 2)), min_support=1)
    out = select(model, {})
    assert out.chased == 0 and out.skipped == 0
    assert out.skipped_value is None and out.summary()["skipped_value"] is None


def test_selection_carries_its_estimate_for_the_audit_trail():
    c = ctx()
    model = fit(rows(c, treated=(100, 60), control=(100, 20)), min_support=10)
    sel = decide(model, c)
    assert isinstance(sel, Selection)
    assert sel.estimate.segment is Segment.PERSUADABLE
    assert "uplift" in sel.reason
