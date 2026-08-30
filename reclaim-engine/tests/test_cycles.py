"""Phase 26 tests — the learn-then-target demo over two periods.

This demo makes the strongest claim in the project: that targeting on uplift
turns a value-destroying recovery campaign into a value-creating one. So these
tests attack the claim from both sides — that the model really did discover the
behaviours from data rather than being handed them, and that the improvement is
not an artefact of comparing two different populations.
"""
from decimal import Decimal

import pytest

from reclaim import cycles as C
from reclaim.measurement import Arm
from reclaim.money import Money
from reclaim.pipeline import Targeting
from reclaim.scorecard import build_scorecard
from reclaim.uplift import Segment

D = Decimal


@pytest.fixture(scope="module")
def run():
    """The whole two-period experiment, computed once (~0.3s)."""
    first, second, model = C.run_cycles()
    return first, second, model


# --------------------------------------------------------------------------
# The fixture's own machinery
# --------------------------------------------------------------------------
def test_behaviours_are_balanced_across_the_cohort():
    assert C.COHORT_SIZE % len(C.BEHAVIOURS) == 0
    counts = {}
    for i in range(C.COHORT_SIZE):
        name = C.behaviour_for(i).name
        counts[name] = counts.get(name, 0) + 1
    assert set(counts.values()) == {C.COHORT_SIZE // len(C.BEHAVIOURS)}


def test_uplift_pp_reports_the_authored_difference():
    assert [b.uplift_pp for b in C.BEHAVIOURS] == [5, 40, -30]


def test_the_outcome_draw_is_deterministic_and_hits_its_rate():
    assert C._resolves("u1", 65, salt="s") is C._resolves("u1", 65, salt="s")
    hits = sum(1 for i in range(2000) if C._resolves(f"u{i}", 65, salt="s"))
    assert 1200 < hits < 1400                     # 65% of 2000


def test_the_two_cycles_draw_independently():
    """Different salts, or cycle 2 would be the same month twice."""
    differing = sum(1 for i in range(500)
                    if C._resolves(f"u{i}", 50, salt="followup-cycle-1")
                    != C._resolves(f"u{i}", 50, salt="followup-cycle-2"))
    assert differing > 150


def test_the_outcome_draw_is_independent_of_cohort_assignment():
    """A shared salt would correlate arm with outcome and manufacture a lift."""
    agree = sum(1 for i in range(600)
                if (C.HOLDOUT.arm(f"leak:short:{C._sid(i)}") is Arm.CONTROL)
                == C._resolves(f"leak:short:{C._sid(i)}", 30, salt="followup-cycle-1"))
    assert 200 < agree < 400


def test_the_cohort_is_short_paid_by_its_behaviours_shortfall():
    settlements, banks = C.build_cohort(ts=C.T0)
    assert len(settlements) == len(banks) == C.COHORT_SIZE
    for i in (0, 1, 2):
        expected = Money.of(C.GROSS, "INR") - Money.of(C.behaviour_for(i).shortfall, "INR")
        assert banks[i].gross_amount == expected


def test_a_resolved_settlement_is_credited_in_full():
    _s, banks = C.build_cohort(resolved_settlement_ids={C._sid(0)}, ts=C.T0)
    assert banks[0].gross_amount == Money.of(C.GROSS, "INR")
    assert banks[1].gross_amount < Money.of(C.GROSS, "INR")


def test_the_context_exposes_only_the_leak_amount_as_signal():
    settlements, banks = C.build_cohort(ts=C.T0)
    from reclaim.pipeline import run_reclaim
    rep = run_reclaim(settlements, banks)
    ctxs = [C.context_for(l) for l in rep.exact.leaks[:3]]
    assert {c.days_since_failure for c in ctxs} == {1}
    assert {c.prior_failures for c in ctxs} == {0}
    assert len({c.amount for c in ctxs}) == 3          # only the amount varies


# --------------------------------------------------------------------------
# What the model learned — the load-bearing claim
# --------------------------------------------------------------------------
def test_the_model_discovers_all_three_behaviours_unaided(run):
    """It is told nothing about which shortfall means which behaviour. It reads
    them out of one period of observed bank outcomes."""
    _first, _second, model = run
    learned = {b.name: (seg, up) for b, seg, up in C.learned_table(model)}
    assert learned["sure thing"][0] is Segment.SURE_THING
    assert learned["persuadable"][0] is Segment.PERSUADABLE
    assert learned["sleeping dog"][0] is Segment.SLEEPING_DOG


def test_the_learned_uplift_signs_match_the_authored_ones(run):
    _first, _second, model = run
    for behaviour, _segment, uplift in C.learned_table(model):
        assert (uplift > 0) is (behaviour.uplift_pp > 0), behaviour.name


def test_every_learned_estimate_comes_from_its_own_cell(run):
    """If a cell fell back to the pool it would be reading a mixture of all
    three behaviours, which is exactly the failure this cohort size prevents."""
    from reclaim.uplift import BASIS_CELL, Context
    from reclaim.recovery import FailureReason
    _first, _second, model = run
    for behaviour in C.BEHAVIOURS:
        est = model.predict(Context(FailureReason.INSUFFICIENT_FUNDS,
                                    Money.of(behaviour.shortfall, "INR"), 1, 0))
        assert est.basis == BASIS_CELL, behaviour.name


def test_training_labels_come_from_observed_outcomes_not_claims(run):
    """The executor always succeeds, so every treated row would be `True` if the
    labels were claims. They are not."""
    first, _second, _model = run
    rows = C.training_rows(first.prior, first.observed)
    treated = [r for r in rows if r.arm is Arm.TREATED]
    assert len(rows) == C.COHORT_SIZE
    assert any(not r.recovered for r in treated)
    assert any(r.recovered for r in treated)


# --------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------
def test_targeting_makes_far_fewer_contacts(run):
    first, second, _model = run
    assert second.units_contacted < first.units_contacted / 2
    assert len(second.prior.skipped_leaks) > 0
    assert first.prior.skipped_leaks == ()


def test_chasing_everyone_destroys_value_and_targeting_reverses_it(run):
    """The headline. Cycle 1's *claimed* recovery is large and its *causal*
    recovery is negative; cycle 2 makes a third of the contacts and turns it
    positive."""
    first, second, _model = run
    assert first.card.gross_recovered.is_positive
    assert first.causal.is_negative
    assert second.causal.is_positive
    assert second.causal > first.causal


def test_causal_per_contact_improves(run):
    first, second, _model = run
    assert first.causal_per_contact() < 0 < second.causal_per_contact()


def test_the_measured_lift_rises(run):
    first, second, _model = run
    assert second.lift.lift_pp > first.lift.lift_pp
    assert first.lift.underpowered is False and second.lift.underpowered is False


def test_both_arms_still_contain_every_behaviour_in_cycle_two(run):
    """The improvement must not come from comparing two different populations.
    Treated means 'the policy ran', which includes units it chose not to
    contact — so both cohorts keep the full behaviour mix."""
    _first, second, _model = run
    amounts_by_arm = {Arm.TREATED: set(), Arm.CONTROL: set()}
    for obs in second.observed.observations:
        amounts_by_arm[obs.arm].add(str(obs.amount))
    expected = {f"{Decimal(b.shortfall):.2f} INR" for b in C.BEHAVIOURS}
    assert amounts_by_arm[Arm.TREATED] == expected
    assert amounts_by_arm[Arm.CONTROL] == expected


def test_skipped_value_is_reported_not_hidden(run):
    _first, second, _model = run
    assert second.prior.skipped_amount().is_positive
    assert second.summary()["skipped_value"] == str(second.prior.skipped_amount())


def test_a_skipped_leak_stays_on_the_honest_residual_list(run):
    """Declining to chase money does not make it stop being missing."""
    _first, second, _model = run
    skipped_ids = {l.id for l, _r in second.prior.skipped_leaks}
    residual_ids = {l.id for l in second.prior.residual_leaks}
    assert skipped_ids and skipped_ids <= residual_ids


def test_every_unit_is_observed_in_both_cycles(run):
    first, second, _model = run
    for cycle in (first, second):
        assert cycle.observed.unobserved == ()
        assert cycle.observed.observed_count == C.COHORT_SIZE


def test_the_whole_experiment_is_reproducible():
    a1, a2, _m = C.run_cycles()
    b1, b2, _m2 = C.run_cycles()
    assert a1.summary() == b1.summary()
    assert a2.summary() == b2.summary()


# --------------------------------------------------------------------------
# CycleResult shape, including the refusals
# --------------------------------------------------------------------------
def test_summary_is_serialisable(run):
    first, _second, _model = run
    s = first.summary()
    assert s["label"].startswith("cycle 1")
    assert s["contacts"] == first.contacts
    assert s["lift_pp"] is not None and s["causal"] is not None


def test_a_cycle_with_no_measurable_lift_reports_none_throughout(run):
    """Constructed directly: no control cohort, so nothing is attributable."""
    first, _second, _model = run
    card = build_scorecard(first.prior, None)
    barren = C.CycleResult(label="no control", prior=first.prior,
                           followup=first.followup, observed=first.observed,
                           lift=None, card=card)
    assert barren.causal is None
    assert barren.causal_per_contact() is None
    s = barren.summary()
    assert s["lift_pp"] is None and s["observed"] is None
    assert s["causal"] is None and s["causal_per_contact"] is None


def test_a_cycle_that_contacts_nobody_cannot_be_divided(run):
    """Guards the division instead of reporting an infinite efficiency.

    A model with no evidence at all, under a SKIP policy, chases nobody — so the
    cycle runs, measures, and has no contacts to divide by.
    """
    from reclaim.uplift import CellStats, UnknownPolicy, UpliftModel

    blind = UpliftModel({}, {}, CellStats(), min_support=10)
    empty = C.run_cycle("nobody chased",
                        targeting=Targeting(model=blind, context_for=C.context_for,
                                            unknown=UnknownPolicy.SKIP),
                        salt="followup-cycle-1", t0=C.T0, t1=C.T1)
    assert empty.contacts == 0
    assert empty.causal_per_contact() is None
    assert empty.summary()["causal_per_contact"] is None

    first, _second, _model = run
    assert first.contacts > 0                      # the normal case does divide
    assert first.causal_per_contact() is not None
