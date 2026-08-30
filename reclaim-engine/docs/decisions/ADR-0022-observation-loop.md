# ADR-0022 — A recovery counts when the bank data says so, not when the engine does

**Status:** Accepted · 2026-08-31 · completes architecture §9.2, closes the open item left by [ADR-0021](ADR-0021-causal-measurement.md)

## Context

ADR-0021 built the holdout and the lift arithmetic and then stopped at an honest
wall, recorded in its own "what this deliberately does not do": cohorts were
*assigned*, but nobody ever went back to see what happened to them. Both arms
were missing their outcome, so `causal_recovered` was permanently `null` and the
scorecard's headline metric was structurally unreachable.

Worse, the treated arm had a silent measurement bug waiting in it. The only
signal available was `RecoveryOutcome.final_state is RECOVERED`, which means
"the executor returned success" — a statement about an API call, not about money
in a bank account. Had we scored the experiment on that, treated units would
have been judged by the engine's own claim while control units were judged by
bank data. Asymmetric evidence between arms makes the difference between them
meaningless, and it biases in the flattering direction every time.

## Decision

An `observation` module that scores a prior run's experiment against a **later
batch's reconciliation**, plus a runnable `experiment` command that exercises
the full two-period loop.

**Only observed resolution counts.** A unit is recovered when the follow-up run
no longer lists its leak as residual. The engine's `RECOVERED` claim is not used
as an outcome for either arm. This makes the two arms symmetric — both judged by
the same bank file — which is the only thing that makes their difference mean
anything.

**The engine's claim is kept, as a check on itself.** Where the engine said
`RECOVERED` and the follow-up batch disagrees, the unit is listed in
`claimed_not_observed`. That is a recovery product auditing its own success
metric, and it is the number a buyer should ask about first. In the shipped
demo it fires for 42 of 135 treated units.

**Intention-to-treat, not per-protocol.** A treated unit that halted for a
human, hit the AFA ceiling or exhausted its attempts stays in the treated cohort
with `recovered=False`. Dropping such units would measure "how well recovery
works when it runs" — a number improvable by refusing to run.

**Arms are read from what the run did, not re-derived.** Control units are the
ones `run_reclaim` actually held back (`report.control_leaks`). A `HoldoutPolicy`
supplied after the fact could disagree with the run; the run is ground truth
about which units were left alone.

**"We stopped looking" is not "the money arrived."** A unit whose settlement is
absent from the follow-up batch is reported in `unobserved` with a reason code,
never scored as resolved. Superseded leaks — resolved by a later fuzzy or AI
match — are excluded from both cohorts entirely; they were never missing money,
and including them would dilute both arms with units that had nothing to
recover.

**The follow-up contract is stated, not assumed.** Reconciliation rolls open
items forward: the T+1 file re-presents settlements that had not tied out
alongside the *cumulative* credits against them. Leak ids are pure functions of
the settlement id (`leak:short:s2`), so unit identity is stable across runs and
the comparison is exact rather than heuristic.

## What this deliberately does not do

The demo's two batches are **synthetic**, and the T+1 fixture's outcome rates
are authored (65% treated / 40% control), so the lift the command prints is a
property of the fixture, not evidence about real payments. RECLAIM has still
never measured lift on production data — what now exists is the machinery to,
wired end to end and refusing to report when it cannot measure. The fixture's
outcome draw uses a different salt from the holdout's, so assignment and outcome
are independent; a shared salt would manufacture a lift out of correlated
hashes, and a test pins that they do not correlate.

No time window is modelled. "By T+1" means "in the batch you handed me", and the
caller chooses what that batch covers. A real deployment would want a stated
observation window and a rule for units still in flight at its close.

## Tested by

`tests/test_observation.py` (28) and `tests/test_experiment.py` (17). The ones
that matter are the attempts to make the module lie: a recovery the follow-up
batch never confirms must score as *not* recovered and be flagged
(`test_resolution_is_read_from_the_followup_not_from_the_engine`,
`test_full_loop_catches_a_recovery_the_bank_data_never_confirms`); a unit that
fell out of the batch must not count as a win
(`test_a_unit_not_carried_forward_is_unobserved_not_recovered`); removing the
control group must produce `None` rather than a better-looking number
(`test_removing_the_control_group_reports_none_not_a_better_number`); and
`test_claimed_exceeds_observed_exceeds_causal` pins the ordering of the three
recovery figures that the whole loop exists to separate — ₹27,000 claimed,
₹18,600 observed, ₹7,384.20 causal.
