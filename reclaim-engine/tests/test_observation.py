"""Phase 24 tests — the T+1 observation loop (architecture §9.2, goal G9).

The properties that matter here are not "does the code run" but "can this
module be talked into reporting a lift it did not observe". Most of these tests
try to do exactly that.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from reclaim.domain import (
    Fees,
    LeakRecord,
    LeakType,
    RecoveryState,
    Source,
    Transaction,
    TransactionRefs,
)
from reclaim.ledger import Ledger
from reclaim.measurement import Arm
from reclaim.money import Money
from reclaim.observation import (
    CURRENCY_MISMATCH,
    NOT_CARRIED_FORWARD,
    ObservationError,
    ObservationReport,
    ObservedUnit,
    Unobserved,
    covered_settlement_ids,
    measure_followup_lift,
    observe_followup,
    open_leak_ids,
    units_under_observation,
)
from reclaim.pipeline import RunReport
from reclaim.reconciliation import MatchPair, ReconciliationResult
from reclaim.recovery import (
    AlwaysFailsExecutor,
    AlwaysSucceedsExecutor,
    Channel,
    FailureReason,
    RecoveryAttempt,
    RecoveryEngine,
    RecoveryOutcome,
)

TS = datetime(2026, 8, 25, 9, 0, 0)
D = Decimal


def inr(x: str) -> Money:
    return Money.of(x, "INR")


# --------------------------------------------------------------------------
# Builders — synthetic RunReports give precise control over each branch
# --------------------------------------------------------------------------
def leak(sid: str, amount: str = "200.00", *, recoverable: bool = True,
         leak_type: LeakType = LeakType.SHORT_PAYMENT,
         refs: tuple = None) -> LeakRecord:
    return LeakRecord(
        id=f"leak:short:{sid}", amount=inr(amount), leak_type=leak_type,
        source_refs=(sid,) if refs is None else refs,
        hypothesis="synthetic", recoverable=recoverable,
    )


def settlement(sid: str, gross: str = "3000.00", utr: str = None) -> Transaction:
    return Transaction(id=sid, source=Source.SETTLEMENT, gross_amount=inr(gross), ts=TS,
                       refs=TransactionRefs(utr=utr or f"UTR-{sid}"))


def bank(bid: str, gross: str, utr: str) -> Transaction:
    return Transaction(id=bid, source=Source.BANK, gross_amount=inr(gross), ts=TS,
                       refs=TransactionRefs(utr=utr))


def recovery(l: LeakRecord, state: RecoveryState) -> RecoveryOutcome:
    return RecoveryOutcome(leak=l, final_state=state, attempts=(),
                           recovered_amount=l.amount if state is RecoveryState.RECOVERED else None,
                           rationale="synthetic", notice_at=TS)


def run(*, leaks=(), residual=(), recoveries=(), control=(), matched=(),
        currency: str = "INR") -> RunReport:
    """A RunReport with only the fields the observation loop reads."""
    exact = ReconciliationResult(
        matched=tuple(matched), leaks=tuple(leaks), currency=currency,
        total_settlements=len(leaks) + len(matched), total_expected=inr("10000.00"),
    )
    return RunReport(
        currency=currency, total_expected=inr("10000.00"), matched_amount=inr("0"),
        recovered_amount=inr("0"), residual_leaks=tuple(residual), auto_matched=(),
        ai_outcomes=(), pending_review=(), recoveries=tuple(recoveries), exact=exact,
        ledger=Ledger(), control_leaks=tuple(control),
    )


# --------------------------------------------------------------------------
# ObservedUnit / Unobserved validation
# --------------------------------------------------------------------------
def test_observed_unit_validates_its_inputs():
    l = leak("s1")
    with pytest.raises(ObservationError):
        ObservedUnit(leak="not a leak", arm=Arm.TREATED, engine_claimed_recovered=False)
    with pytest.raises(ObservationError):
        ObservedUnit(leak=l, arm="treated", engine_claimed_recovered=False)
    with pytest.raises(ObservationError):
        ObservedUnit(leak=l, arm=Arm.TREATED, engine_claimed_recovered="yes")
    ok = ObservedUnit(leak=l, arm=Arm.TREATED, engine_claimed_recovered=True)
    assert ok.arm is Arm.TREATED


# --------------------------------------------------------------------------
# Reading a run
# --------------------------------------------------------------------------
def test_covered_settlement_ids_is_a_complete_census():
    matched = (MatchPair(settlement=settlement("s1"), bank=bank("b1", "3000.00", "UTR-s1"),
                         utr="UTR-s1"),)
    leaks = (leak("s2"), leak("s3", recoverable=False, leak_type=LeakType.MISSING_SETTLEMENT))
    assert covered_settlement_ids(run(matched=matched, leaks=leaks)) == {"s1", "s2", "s3"}


def test_timing_leaks_do_not_pollute_the_settlement_census():
    """A TIMING leak names a *bank* credit; counting it as a settlement would
    make an unrelated unit look carried-forward."""
    timing = LeakRecord(id="leak:unexpected:b9", amount=inr("500.00"),
                        leak_type=LeakType.TIMING, source_refs=("b9",),
                        hypothesis="orphan credit", recoverable=False)
    assert covered_settlement_ids(run(leaks=(timing,))) == frozenset()


def test_a_leak_with_no_source_refs_contributes_nothing_to_the_census():
    anon = LeakRecord(id="leak:anon", amount=inr("1.00"), leak_type=LeakType.SHORT_PAYMENT,
                      source_refs=(), hypothesis="no refs", recoverable=True)
    assert covered_settlement_ids(run(leaks=(anon,))) == frozenset()


def test_open_leak_ids_is_the_honest_residual():
    l1, l2 = leak("s1"), leak("s2")
    assert open_leak_ids(run(leaks=(l1, l2), residual=(l2,))) == {"leak:short:s2"}


# --------------------------------------------------------------------------
# Who is in the experiment
# --------------------------------------------------------------------------
def test_non_recoverable_leaks_are_not_experimental_units():
    l = leak("s1", recoverable=False, leak_type=LeakType.MISSING_SETTLEMENT)
    assert units_under_observation(run(leaks=(l,), residual=(l,))) == ()


def test_superseded_leaks_are_excluded_from_both_cohorts():
    """A leak a later fuzzy match resolved was never missing money. Leaving it
    in would dilute the cohorts with units that had nothing to recover."""
    l = leak("s1")
    r = run(leaks=(l,), residual=(), recoveries=(), control=())
    assert units_under_observation(r) == ()


def test_arms_come_from_what_the_run_actually_did():
    treated, held = leak("s1"), leak("s2")
    r = run(leaks=(treated, held), residual=(treated,), control=(held,))
    units = units_under_observation(r)
    assert [(u.leak.id, u.arm) for u in units] == [
        ("leak:short:s1", Arm.TREATED),
        ("leak:short:s2", Arm.CONTROL),
    ]


def test_a_treated_unit_that_halted_stays_in_the_treated_cohort():
    """Intention-to-treat. Dropping the halted units would measure 'how well
    recovery works when it runs' — improvable by refusing to run."""
    l = leak("s1")
    r = run(leaks=(l,), residual=(l,), recoveries=(recovery(l, RecoveryState.HALTED),))
    units = units_under_observation(r)
    assert len(units) == 1
    assert units[0].arm is Arm.TREATED and units[0].engine_claimed_recovered is False


def test_engine_claim_is_recorded_but_kept_separate():
    l = leak("s1")
    r = run(leaks=(l,), recoveries=(recovery(l, RecoveryState.RECOVERED),))
    assert units_under_observation(r)[0].engine_claimed_recovered is True


# --------------------------------------------------------------------------
# The observation itself
# --------------------------------------------------------------------------
def test_resolution_is_read_from_the_followup_not_from_the_engine():
    """The engine says RECOVERED; the follow-up batch still shows the leak.
    The observation must record not-recovered, and flag the disagreement."""
    l = leak("s1")
    prior = run(leaks=(l,), recoveries=(recovery(l, RecoveryState.RECOVERED),))
    later = run(leaks=(l,), residual=(l,))          # still open at T+1
    rep = observe_followup(prior, later)
    assert rep.observations[0].recovered is False
    assert rep.claimed_not_observed == ("leak:short:s1",)


def test_a_claim_the_followup_confirms_is_not_flagged():
    l = leak("s1")
    prior = run(leaks=(l,), recoveries=(recovery(l, RecoveryState.RECOVERED),))
    later = run(leaks=(l,), residual=())            # resolved at T+1
    rep = observe_followup(prior, later)
    assert rep.observations[0].recovered is True
    assert rep.claimed_not_observed == ()


def test_a_unit_not_carried_forward_is_unobserved_not_recovered():
    """'We stopped looking' and 'the money arrived' must not collapse into one
    number. Absent from the follow-up batch means unknown."""
    l = leak("s1")
    prior = run(leaks=(l,), residual=(l,))
    later = run(leaks=(leak("s9"),), residual=())   # s1 nowhere in the T+1 file
    rep = observe_followup(prior, later)
    assert rep.observations == ()
    assert rep.unobserved == (Unobserved("leak:short:s1", Arm.TREATED, NOT_CARRIED_FORWARD),)


def test_a_unit_with_no_source_refs_cannot_be_traced_forward():
    anon = LeakRecord(id="leak:anon", amount=inr("100.00"), leak_type=LeakType.SHORT_PAYMENT,
                      source_refs=(), hypothesis="no refs", recoverable=True)
    prior = run(leaks=(anon,), residual=(anon,))
    rep = observe_followup(prior, run(leaks=(leak("s1"),), residual=()))
    assert rep.unobserved[0].reason == NOT_CARRIED_FORWARD


def test_a_followup_in_another_currency_is_refused_per_unit():
    l = leak("s1")
    prior = run(leaks=(l,), residual=(l,))
    later = run(leaks=(), residual=(), currency="USD")
    rep = observe_followup(prior, later)
    assert rep.observations == ()
    assert rep.unobserved[0].reason == CURRENCY_MISMATCH


def test_a_run_with_no_recoverable_leaks_has_no_experiment_to_score():
    clean = run(leaks=(), residual=())
    with pytest.raises(ObservationError):
        observe_followup(clean, clean)


# --------------------------------------------------------------------------
# Measurement gating — the refusals
# --------------------------------------------------------------------------
def _two_arm_runs(n_treated=2, n_control=2, treated_recovered=2, control_recovered=0):
    """Prior + follow-up pair with a known outcome in each arm."""
    treated = [leak(f"t{i}") for i in range(n_treated)]
    control = [leak(f"c{i}") for i in range(n_control)]
    prior = run(leaks=tuple(treated + control), residual=tuple(treated),
                control=tuple(control))
    unresolved = ([l for l in treated[treated_recovered:]]
                  + [l for l in control[control_recovered:]])
    later = run(leaks=tuple(treated + control), residual=tuple(unresolved))
    return prior, later


def test_one_arm_only_is_not_measurable():
    l = leak("s1")
    prior = run(leaks=(l,), residual=(l,))
    later = run(leaks=(l,), residual=())
    rep = observe_followup(prior, later)
    assert rep.is_measurable() is False
    assert measure_followup_lift(prior, later) is None


def test_both_arms_observed_produces_a_real_lift():
    prior, later = _two_arm_runs(n_treated=2, n_control=2,
                                 treated_recovered=2, control_recovered=0)
    rep = observe_followup(prior, later)
    assert rep.is_measurable() is True
    lift = measure_followup_lift(prior, later, min_cohort=1)
    assert lift is not None
    assert lift.lift_pp == D("100.0000")        # 100% treated vs 0% control
    assert lift.underpowered is False


def test_a_worthless_intervention_measures_as_worthless():
    """Both arms resolve at the same rate: the correct answer is zero lift, and
    the gross recovery must not be creditable."""
    prior, later = _two_arm_runs(n_treated=2, n_control=2,
                                 treated_recovered=2, control_recovered=2)
    lift = measure_followup_lift(prior, later, min_cohort=1)
    assert lift.lift_pp == D("0.0000")
    assert lift.incremental_amount == inr("0")


def test_a_harmful_intervention_reports_negative_lift():
    prior, later = _two_arm_runs(n_treated=2, n_control=2,
                                 treated_recovered=0, control_recovered=2)
    lift = measure_followup_lift(prior, later, min_cohort=1)
    assert lift.lift_pp == D("-100.0000")


def test_small_cohorts_are_flagged_underpowered_not_hidden():
    prior, later = _two_arm_runs()
    lift = measure_followup_lift(prior, later)      # default min_cohort=30
    assert lift.underpowered is True
    assert "UNDERPOWERED" in lift.note


def test_report_with_no_observations_measures_to_none():
    empty = ObservationReport(observations=(), unobserved=(), claimed_not_observed=())
    assert empty.measure() is None
    assert empty.is_measurable() is False


def test_summary_states_what_was_and_was_not_observed():
    prior, later = _two_arm_runs()
    orphan = leak("gone")
    prior = run(leaks=tuple(list(prior.exact.leaks) + [orphan]),
                residual=tuple(list(prior.residual_leaks) + [orphan]),
                control=prior.control_leaks)
    rep = observe_followup(prior, later)
    s = rep.summary()
    assert s["observed"] == 4 and s["treated_observed"] == 2 and s["control_observed"] == 2
    assert s["unobserved"] == 1
    assert s["unobserved_reasons"] == {"leak:short:gone": NOT_CARRIED_FORWARD}
    assert s["measurable"] is True
    assert s["claimed_not_observed"] == []


def test_by_arm_partitions_the_observations():
    prior, later = _two_arm_runs()
    rep = observe_followup(prior, later)
    assert len(rep.by_arm(Arm.TREATED)) == 2
    assert len(rep.by_arm(Arm.CONTROL)) == 2
    assert rep.observed_count == 4 and rep.unobserved_count == 0


# --------------------------------------------------------------------------
# End to end through the real pipeline
# --------------------------------------------------------------------------
def _short_pay_batch(sids, *, resolved):
    """Settlements with a ₹200 shortfall; ``resolved`` ids are credited in full."""
    settlements = [settlement(s) for s in sids]
    banks = [bank(f"b{s}", "3000.00" if s in resolved else "2800.00", f"UTR-{s}")
             for s in sids]
    return settlements, banks


def test_full_loop_measures_lift_from_two_real_runs():
    """Prior run with a 50% holdout, then a genuine follow-up reconciliation.
    Nothing synthetic about the arithmetic — only the fixture data."""
    from reclaim.measurement import HoldoutPolicy
    from reclaim.pipeline import run_reclaim

    sids = [f"s{i:02d}" for i in range(20)]
    s_t0, b_t0 = _short_pay_batch(sids, resolved=set())
    holdout = HoldoutPolicy(control_pct=50, salt="test-obs")
    prior = run_reclaim(s_t0, b_t0, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                        base_time=TS, holdout=holdout)

    treated = {l.id for l in prior.exact.leaks} - {l.id for l in prior.control_leaks}
    assert treated and prior.control_leaks               # both arms non-empty

    # At T+1 every treated settlement was topped up; no control one was.
    resolved = {sid for sid in sids if f"leak:short:{sid}" in treated}
    s_t1, b_t1 = _short_pay_batch(sids, resolved=resolved)
    later = run_reclaim(s_t1, b_t1)

    rep = observe_followup(prior, later)
    assert rep.unobserved == ()
    lift = rep.measure(min_cohort=1)
    assert lift.lift_pp == D("100.0000")
    assert lift.treated.n == len(treated) and lift.control.n == len(prior.control_leaks)
    assert rep.claimed_not_observed == ()


def test_full_loop_catches_a_recovery_the_bank_data_never_confirms():
    """The engine's executor succeeds for every unit, but the follow-up batch
    shows no money. Every claim must be flagged, and the lift must be zero."""
    from reclaim.measurement import HoldoutPolicy
    from reclaim.pipeline import run_reclaim

    sids = [f"s{i:02d}" for i in range(10)]
    s_t0, b_t0 = _short_pay_batch(sids, resolved=set())
    prior = run_reclaim(s_t0, b_t0, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                        base_time=TS, holdout=HoldoutPolicy(control_pct=50, salt="test-obs"))
    s_t1, b_t1 = _short_pay_batch(sids, resolved=set())     # nothing arrived
    later = run_reclaim(s_t1, b_t1)

    rep = observe_followup(prior, later)
    treated = [o for o in rep.observations if o.arm is Arm.TREATED]
    assert len(rep.claimed_not_observed) == len(treated)
    assert rep.measure(min_cohort=1).lift_pp == D("0.0000")


def test_a_failed_recovery_makes_no_claim_to_contradict():
    from reclaim.measurement import HoldoutPolicy
    from reclaim.pipeline import run_reclaim

    sids = [f"s{i:02d}" for i in range(10)]
    s_t0, b_t0 = _short_pay_batch(sids, resolved=set())
    prior = run_reclaim(s_t0, b_t0, recovery_engine=RecoveryEngine(AlwaysFailsExecutor()),
                        base_time=TS, holdout=HoldoutPolicy(control_pct=50, salt="test-obs"))
    s_t1, b_t1 = _short_pay_batch(sids, resolved=set())
    later = run_reclaim(s_t1, b_t1)
    assert observe_followup(prior, later).claimed_not_observed == ()


def test_fees_in_the_followup_do_not_break_the_census():
    """Coverage is read from settlement ids, so a fee-bearing follow-up batch
    still traces units forward."""
    from reclaim.pipeline import run_reclaim

    l = leak("s1")
    prior = run(leaks=(l,), residual=(l,))
    s = Transaction(id="s1", source=Source.SETTLEMENT, gross_amount=inr("3000.00"), ts=TS,
                    fees=Fees(inr("100.00"), inr("18.00"), inr("0"), inr("0")),
                    refs=TransactionRefs(utr="UTR-s1"))
    later = run_reclaim([s], [bank("b1", "2882.00", "UTR-s1")])
    rep = observe_followup(prior, later)
    assert rep.observations[0].recovered is True     # tied out in full at T+1


# --------------------------------------------------------------------------
# Post-Sprint-3 — the bridge to offline policy evaluation
# --------------------------------------------------------------------------
def _bandit_run(*, epsilon="0.40", n=300, whatsapp_pct=70, upi_pct=30,
                control_pct=20):
    """A real pipeline run with a bandit choosing the channel, plus its T+1 batch.

    WhatsApp genuinely works better in the fixture, so the log contains a signal
    a policy could learn — and the reward comes from the follow-up batch, never
    from the executor (which always succeeds).
    """
    import hashlib
    from datetime import timedelta
    from decimal import Decimal
    from reclaim.bandit import Action, EpsilonGreedyBandit, EpsilonGreedyConfig
    from reclaim.measurement import HoldoutPolicy
    from reclaim.pipeline import run_reclaim
    from reclaim.recovery import Channel

    actions = [Action(Channel.UPI_RETRY, 10, "firm"),
               Action(Channel.WHATSAPP_NUDGE, 10, "gentle")]
    bandit = EpsilonGreedyBandit(actions,
                                 config=EpsilonGreedyConfig(epsilon=Decimal(epsilon)))

    def batch(resolved=frozenset(), ts=TS):
        s, b = [], []
        for i in range(n):
            sid = f"s{i:03d}"
            s.append(Transaction(id=sid, source=Source.SETTLEMENT,
                                 gross_amount=inr("3000.00"), ts=ts,
                                 refs=TransactionRefs(utr=f"U{sid}"),
                                 counterparty=f"cust-{i}"))
            b.append(Transaction(id=f"b{sid}", source=Source.BANK,
                                 gross_amount=inr("3000.00" if sid in resolved
                                                  else "2800.00"), ts=ts,
                                 refs=TransactionRefs(utr=f"U{sid}")))
        return s, b

    engine = RecoveryEngine(
        AlwaysSucceedsExecutor(),
        action_policy=lambda l, attempt, at: bandit.choose("short-payment", l.id))
    s0, b0 = batch()
    prior = run_reclaim(s0, b0, recovery_engine=engine, base_time=TS,
                        holdout=HoldoutPolicy(control_pct=control_pct, salt="bandit"))

    def draw(uid):
        return int.from_bytes(hashlib.sha256(f"o:{uid}".encode()).digest()[:8],
                              "big") % 100

    resolved = set()
    for r in prior.recoveries:
        rate = (whatsapp_pct if r.attempts[0].channel is Channel.WHATSAPP_NUDGE
                else upi_pct)
        if draw(r.leak.id) < rate:
            resolved.add(r.leak.source_refs[0])
    for l in prior.control_leaks:
        if draw(l.id) < 25:
            resolved.add(l.source_refs[0])
    s1, b1 = batch(resolved, TS + timedelta(days=30))
    return bandit, actions, prior, run_reclaim(s1, b1)


def test_logged_decisions_reward_comes_from_the_bank_not_the_executor():
    """The executor always succeeds. If rewards were the executor's result every
    row would be 1, and a policy trained on that would rank whatever the rail
    accepts — a fact about the gateway, not about customers."""
    from reclaim.observation import logged_decisions

    _b, _a, prior, later = _bandit_run()
    log = logged_decisions(prior, observe_followup(prior, later))
    rewards = {r.reward for r in log}
    assert rewards == {D("0"), D("1")}
    assert all(r.final_state.value == "recovered" for r in prior.recoveries)


def test_the_ips_identity_holds_on_a_log_the_engine_produced():
    """The end-to-end check: a log assembled from real attempts must reproduce
    its own mean when the logging policy is evaluated."""
    from reclaim.bandit import LoggingPolicyEcho
    from reclaim.observation import logged_decisions
    from reclaim.offline_eval import ips

    bandit, actions, prior, later = _bandit_run()
    log = logged_decisions(prior, observe_followup(prior, later))
    keys = [a.key for a in actions]
    estimate = ips(log, LoggingPolicyEcho(bandit).probability, actions=keys)
    assert estimate.identified is True
    assert estimate.value == estimate.logged_mean


def test_a_better_policy_is_learned_and_then_graded_before_deployment():
    """The whole point of Phase 27, exercised through the real engine."""
    from reclaim.bandit import GreedyPolicy
    from reclaim.observation import logged_decisions
    from reclaim.offline_eval import ips, should_deploy
    from reclaim.recovery import Channel

    bandit, actions, prior, later = _bandit_run()
    log = logged_decisions(prior, observe_followup(prior, later))
    learned = bandit.observe_all([
        (r.context_key, next(a for a in actions if a.key == r.action_key),
         r.reward == D("1")) for r in log])
    assert learned.greedy("short-payment").channel is Channel.WHATSAPP_NUDGE

    verdict = should_deploy(ips(log, GreedyPolicy(learned).probability,
                                actions=[a.key for a in actions]))
    assert verdict.ship is True
    assert "estimated improvement" in verdict.reason


def test_only_the_first_attempt_of_a_sequence_is_logged():
    """Later attempts in a sequence are not independent draws."""
    from reclaim.bandit import Action, EpsilonGreedyBandit
    from reclaim.observation import logged_decisions
    from reclaim.recovery import AlwaysFailsExecutor, Channel, RecoveryConfig

    l = leak("s1")
    bandit = EpsilonGreedyBandit([Action(Channel.UPI_RETRY, 10, "firm")])
    engine = RecoveryEngine(AlwaysFailsExecutor(), RecoveryConfig(max_attempts=3),
                            action_policy=lambda lk, a, at: bandit.choose("ctx", lk.id))
    outcome = engine.recover(l, FailureReason.INSUFFICIENT_FUNDS, TS)
    assert len(outcome.attempts) == 3

    prior = run(leaks=(l,), residual=(l,), recoveries=(outcome,))
    later = run(leaks=(l,), residual=())
    assert len(logged_decisions(prior, observe_followup(prior, later))) == 1


def test_attempts_with_no_decision_record_are_skipped():
    """An attempt with no logged propensity cannot be evaluated offline, and
    inventing one afterwards is the bias ADR-0027 refuses."""
    from reclaim.observation import logged_decisions

    l = leak("s1")
    plain = recovery(l, RecoveryState.RECOVERED)      # no attempts at all
    prior = run(leaks=(l,), residual=(), recoveries=(plain,))
    later = run(leaks=(l,), residual=())
    assert logged_decisions(prior, observe_followup(prior, later)) == ()

    unpolicied = RecoveryOutcome(
        leak=l, final_state=RecoveryState.EXHAUSTED,
        attempts=(RecoveryAttempt(0, Channel.UPI_RETRY, TS, "k", "failed"),),
        recovered_amount=None, rationale="x", notice_at=TS)
    prior = run(leaks=(l,), residual=(l,), recoveries=(unpolicied,))
    assert logged_decisions(prior, observe_followup(prior, run(leaks=(l,), residual=(l,)))) == ()


def test_a_unit_that_was_never_observed_contributes_no_row():
    from reclaim.observation import logged_decisions

    l = leak("s1")
    outcome = RecoveryOutcome(
        leak=l, final_state=RecoveryState.RECOVERED,
        attempts=(RecoveryAttempt(0, Channel.UPI_RETRY, TS, "k", "succeeded",
                                  action_key="upi_retry|10|firm", message="firm",
                                  propensity=D("0.9"), context_key="ctx"),),
        recovered_amount=l.amount, rationale="x", notice_at=TS)
    prior = run(leaks=(l,), residual=(l,), recoveries=(outcome,))
    later = run(leaks=(leak("s9"),), residual=())      # s1 not carried forward
    report = observe_followup(prior, later)
    assert report.observations == ()
    assert logged_decisions(prior, report) == ()
