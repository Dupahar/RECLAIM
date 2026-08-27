# ADR-0021 — Recovery is credited causally or not at all

**Status:** Accepted · 2026-08-28 · implements architecture §9.2 and §9.3, satisfies goal **G9**

## Context

G9 — "recovery impact measured against a control, not asserted" — was the
architecture's one wholly unimplemented goal. The engine reported gross recovery
and nothing else.

Gross recovery is close to meaningless as a product claim. Some of that money
would have arrived anyway: a customer tops up and the next debit succeeds
without any intervention. A recovery tool that reports gross is taking credit
for the payment system working.

## Decision

A `measurement` module and a `scorecard` module.

**Deterministic holdout.** `HoldoutPolicy` assigns a unit to control by hashing
`salt + unit_id` — stable, uniform, reproducible on any machine, and independent
across experiments as the salt changes. Randomised assignment was rejected: the
engine forbids `random` (G4), and an assignment that moved between runs would
make a run unreplayable.

**Control leaks are genuinely not chased.** `run_reclaim(holdout=...)` skips
them entirely and records them in `report.control_leaks`. This costs real money
and is the price of being able to say anything true about impact.

**The measurement refuses more than it reports.** With no control cohort the
lift is `None` and the note says "undefined, not zero" — never a flattering
100%. Below `min_cohort` the result is flagged `underpowered`. Mixed currencies
raise. A negative lift is reported as negative.

**Incremental, not gross.** `incremental_amount` is treated recovery minus what
the treated cohort would be expected to recover with no intervention, estimated
from the control cohort's own value-recovery rate. On the worked example: gross
₹6,000, causal ₹3,500.

**The scorecard is ungameable by construction** (§9.3). Match rate and closure
rate, gross *and* causal recovery, the residual, conduct metrics
(`contacts_per_unit`, `wasted_contact_rate`, `notice_compliance`,
`halted_for_human`) and `time_to_closure_hours`. Loosen matching and the
residual and conduct numbers move; chase harder and contact metrics move; skip
the control group and the causal figure blanks itself.

## What this deliberately does not do

The module measures observations; it does not fabricate them. Control outcomes
must be **observed** — a held-out leak that resolved on its own becomes visible
when a later batch re-reconciles — and the caller supplies them. Computing a
lift from a control group whose outcomes were never observed would be precisely
the kind of assertion this project exists to avoid, so the wiring to
longitudinal outcomes is left for the T+1 re-reconciliation loop rather than
faked now.

Offline policy evaluation (IPS / doubly-robust) and drift monitors from §9.2
remain unbuilt; they gate a bandit policy, and there is no bandit yet.

## Tested by

`tests/test_measurement.py` (16 tests) and `tests/test_scorecard.py` (9),
including: a worthless intervention measuring as worthless, a harmful one
measuring negative, an absent control reporting `None` rather than a perfect
score, and a 100%-holdout run chasing nothing at all.
