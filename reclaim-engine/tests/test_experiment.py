"""Phase 24 tests — the runnable two-period holdout experiment.

The demo's job is to be *unable* to flatter itself. These tests pin the
properties that keep it honest: the fixture's outcome draw is independent of
cohort assignment, the three recovery numbers stay in the right order, and
removing the control group produces `None` rather than a better-looking result.
"""
from decimal import Decimal

import pytest

from reclaim.domain import LeakRecord, LeakType, RecoveryState
from reclaim.measurement import Arm, HoldoutPolicy
from reclaim.money import Money
from reclaim.observation import observe_followup
from reclaim import experiment as X

D = Decimal


def inr(x: str) -> Money:
    return Money.of(x, "INR")


# --------------------------------------------------------------------------
# The fixture's outcome draw
# --------------------------------------------------------------------------
def test_fixture_outcome_is_deterministic():
    assert X._fixture_resolves("leak:short:s001", 65) is X._fixture_resolves("leak:short:s001", 65)


def test_fixture_rate_is_approximately_the_authored_rate():
    hits = sum(1 for i in range(2000) if X._fixture_resolves(f"u{i}", 65))
    assert 1200 < hits < 1400          # 65% of 2000 = 1300


def test_zero_and_full_rates_are_respected():
    assert not any(X._fixture_resolves(f"u{i}", 0) for i in range(50))
    assert all(X._fixture_resolves(f"u{i}", 100) for i in range(50))


def test_outcome_draw_is_independent_of_cohort_assignment():
    """If assignment and outcome shared a salt, the demo would manufacture a
    lift from correlated hashes rather than measure one."""
    agree = sum(1 for i in range(400)
                if (X.HOLDOUT.arm(f"u{i}") is Arm.CONTROL) == X._fixture_resolves(f"u{i}", 30))
    assert 130 < agree < 270           # no systematic coupling either way


# --------------------------------------------------------------------------
# The batches
# --------------------------------------------------------------------------
def test_cohort_batch_is_short_paid_by_default():
    settlements, banks = X.build_cohort_batch()
    assert len(settlements) == len(banks) == X.COHORT_SIZE
    assert all(b.gross_amount == inr(X.CREDITED) for b in banks)
    assert settlements[0].id == "s000" and settlements[-1].id == f"s{X.COHORT_SIZE - 1:03d}"


def test_resolved_settlements_are_credited_in_full():
    settlements, banks = X.build_cohort_batch(resolved_settlement_ids={"s000", "s005"})
    paid = {b.refs.utr: b.gross_amount for b in banks}
    assert paid["UTR-s000"] == inr(X.GROSS)
    assert paid["UTR-s001"] == inr(X.CREDITED)


def test_prior_run_produces_both_arms_and_serves_a_real_notice():
    prior = X.run_prior()
    assert prior.control_leaks                      # something was held back
    assert prior.recoveries                         # something was chased
    assert all(r.notice_sent for r in prior.recoveries)


def test_followup_ignores_non_recoverable_leaks():
    """A prior whose only leak is an accounting exception has nothing to
    resolve, so the follow-up batch stays entirely short-paid."""
    class _Prior:
        control_leaks = ()

        class exact:
            leaks = (LeakRecord(id="leak:missing:s000", amount=inr("100.00"),
                                leak_type=LeakType.MISSING_SETTLEMENT,
                                source_refs=("s000",), hypothesis="x", recoverable=False),)

    later = X.build_followup(_Prior())
    assert all(l.leak_type is LeakType.SHORT_PAYMENT for l in later.exact.leaks)
    assert len(later.exact.leaks) == X.COHORT_SIZE


def test_followup_resolves_control_and_treated_at_their_authored_rates():
    prior = X.run_prior()
    later = X.build_followup(prior)
    control_ids = {l.id for l in prior.control_leaks}
    still_open = {l.id for l in later.residual_leaks}

    def rate(ids):
        return len([i for i in ids if i not in still_open]) / len(ids)

    treated_ids = [l.id for l in prior.exact.leaks if l.id not in control_ids]
    assert 0.55 < rate(treated_ids) < 0.80
    assert 0.28 < rate(list(control_ids)) < 0.55


# --------------------------------------------------------------------------
# The experiment as a whole
# --------------------------------------------------------------------------
def test_experiment_measures_a_lift_close_to_the_authored_one():
    _prior, _later, observed, lift, _card = X.run_experiment()
    assert observed.unobserved == ()
    expected = X.TREATED_RESOLVE_PCT - X.CONTROL_RESOLVE_PCT
    assert abs(lift.lift_pp - D(expected)) < D("10")     # sampling slack


def test_cohort_is_large_enough_not_to_be_underpowered():
    """The underpowered flag must be capable of being False, or it is decoration."""
    _p, _l, _o, lift, _c = X.run_experiment()
    assert lift.underpowered is False
    assert lift.treated.n >= 30 and lift.control.n >= 30


def test_claimed_exceeds_observed_exceeds_causal():
    """The whole point of the loop. An engine that only reported `claimed`
    would overstate its value by the gap between the first and the last."""
    _prior, _later, _observed, lift, card = X.run_experiment()
    claimed = card.gross_recovered
    observed_amount = lift.treated.recovered_amount
    causal = card.causal_recovered
    assert claimed > observed_amount > causal
    assert causal.is_positive


def test_claims_the_bank_data_contradicts_are_all_flagged():
    _prior, _later, observed, lift, _card = X.run_experiment()
    unconfirmed = lift.treated.n - lift.treated.recovered_n
    assert len(observed.claimed_not_observed) == unconfirmed
    assert unconfirmed > 0


def test_removing_the_control_group_reports_none_not_a_better_number(monkeypatch):
    """With no holdout there is no counterfactual, so there is no lift to
    report — and the scorecard must say so rather than credit the gross."""
    monkeypatch.setattr(X, "HOLDOUT", HoldoutPolicy(control_pct=0, salt="no-control"))
    _prior, _later, observed, lift, card = X.run_experiment()
    assert observed.by_arm(Arm.CONTROL) == ()
    assert lift is None
    assert card.causal_recovered is None
    assert "no holdout was run" in card.causal_note


def test_experiment_is_reproducible():
    a = X.run_experiment()
    b = X.run_experiment()
    assert a[3].lift_pp == b[3].lift_pp
    assert a[4].summary() == b[4].summary()


def test_notice_compliance_is_a_real_number_here():
    """Elsewhere in the demos this is 0 because no notice channel is wired. The
    experiment supplies one, so the metric proves it can move."""
    _p, _l, _o, _lift, card = X.run_experiment()
    assert card.notice_compliance == D("1.0000")


def test_every_treated_unit_is_scored_intention_to_treat():
    prior = X.run_prior()
    later = X.build_followup(prior)
    observed = observe_followup(prior, later)
    chased = [r for r in prior.recoveries if r.final_state is RecoveryState.RECOVERED]
    assert len(observed.by_arm(Arm.TREATED)) == len(chased)
    assert observed.observed_count == X.COHORT_SIZE
