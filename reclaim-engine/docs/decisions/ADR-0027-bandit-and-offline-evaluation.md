# ADR-0027 — ε-greedy, because the evaluator matters more than the sampler

**Status:** Accepted · 2026-08-31 · implements architecture §7 (contextual bandit + IPS/DR) and part of §9.2; **deviates** from the named algorithm

## Context

Two things were outstanding in Layer 5, and they are one requirement: *"Contextual
bandit (Thompson sampling) — learns the best `(timing × channel × message)` per
context, ... with **offline policy evaluation via Inverse Propensity Scoring /
Doubly Robust** so we can validate a new policy **before** deploying it."*

The second half is the one that matters for a system that debits people's
accounts. [ADR-0024](ADR-0024-uplift-targeting.md) and
[ADR-0026](ADR-0026-two-cycle-demonstration.md) both recorded the same gap: a
candidate policy could only be evaluated *after* deployment, by running it on
customers. That is an acceptable way to learn about a recommendation carousel and
not about a debit.

## Decision

Two modules: `bandit` (the policy) and `offline_eval` (the estimators and the
gate).

### ε-greedy instead of Thompson sampling

**This is a deliberate deviation from the architecture's named algorithm.** IPS
and DR divide each logged reward by the probability the logging policy had of
choosing that action. Thompson sampling's action probabilities are an integral
over the posterior: they have to be *estimated*, usually by Monte Carlo over the
sampler. An estimated propensity sits in the *denominator* of the estimator, so
its error is amplified by exactly the factor that makes importance weighting
useful, and the resulting bias cannot be bounded from the log itself.

ε-greedy's propensities are exact and closed-form. Since the architecture asks
for the evaluator as well as the sampler, the sampler that makes the evaluator
honest wins. The cost is real: ε-greedy explores less efficiently than Thompson
and does not narrow exploration as evidence accumulates. Revisit if and when the
volume makes that inefficiency expensive — and if we do, the propensity
estimation error becomes a first-class thing to bound, not a detail.

**Propensities sum to exactly 1.** The non-greedy share is quantized and the
greedy arm absorbs the remainder. Quantizing each arm independently left the
distribution summing to `0.9999` for k=3 — small, and it would make every
importance-weighted estimate a weighted sum against a distribution that is not
one, which is a bias with no upper bound as weights grow. Found by a test
asserting the sum, not by review.

**Exploration is deterministic** — hashed from `salt + context + unit`, as the
holdout assigns cohorts — because `random` is forbidden (G4) and an exploration
draw that moved between runs would make a run unreplayable. **And it is capped**
at ε ≤ 0.5, with the reason in the error message: exploration here means sending
a real customer a message the policy believes is worse. That cost is paid in
somebody's inbox, not just in regret.

**An untried arm reads as ½, not 0** (Beta(1,1) prior). Pessimism about the
untried looks like learning and is a self-fulfilling prophecy — the arm is never
tried again, so it never improves.

**Learning returns a new policy rather than mutating.** A policy that changed
under a caller holding a reference to it would make already-logged propensities
wrong, which is the one thing this module must not do.

### The evaluator refuses more than it reports

An importance-weighted estimator always returns *a* number. Each condition under
which that number is meaningless is checked and named:

- **Zero propensity raises**, at construction of the log row. There is no
  defensible way to divide by it.
- **No overlap means not identified.** If the target policy wants an action the
  log never contains for that context, the estimate is `None` with the
  unsupported contexts listed — not a number with a caveat. This is why `ips`
  requires the full `actions` set: without it the evaluator can only see the
  actions that happen to be in the log, which is precisely the blind spot.
- **Effective sample size is always reported.** An estimate where one row carries
  90% of the weight is a sample of one wearing a sample of a thousand's clothes.
  `ess`, `max_weight_share` and `concentrated` make it visible.
- **Clipping is counted and its direction stated:** variance down, and the
  estimate biased *toward the logging policy*, so a genuinely better target is
  understated. A caller who clips should know which way they have leaned.

`should_deploy` turns an estimate into a ship/don't verdict with four distinct
failure reasons — get coverage, get more data, get a better candidate, or accept
that the deployed policy is fine — because they call for different work.

## What this deliberately does not do

Nothing in the recovery engine calls the bandit yet. `RecoveryConfig.channels`
still rotates deterministically; wiring the bandit in means threading a policy,
a context and a reward signal through `RecoveryEngine`, and the reward is not
available until the T+1 loop closes for that unit. The modules are real, tested
and validated against known truth; the integration is the next phase, and saying
so is better than a half-wired path.

There is no drift monitor. §9.2 asks for one, and the estimators here are the
right substrate for it, but nothing yet re-evaluates a deployed policy on a
rolling window and alarms when its advantage evaporates.

The reward model DR needs is supplied by the caller. No reward model is fitted —
the `uplift` module is the obvious candidate and connecting them is not done.

## Tested by

`tests/test_bandit.py` (32) and `tests/test_offline_eval.py` (28). The estimators
are graded against a known truth (`TRUE_REWARD`), which is the only way to know
an evaluator works: `test_evaluating_the_logging_policy_returns_the_logs_own_mean`
is the identity everything else rests on;
`test_ips_recovers_a_known_value_when_the_log_covers_it_well` checks accuracy
under good coverage; `test_dr_survives_corrupted_propensities_when_the_model_is_
right` is the property DR exists for, and the situation a real deployment is in.

`test_dr_has_lower_error_than_ips_across_independent_draws` replaced an earlier
test asserting DR beats IPS on a *single* thinly-explored log. That claim is
false — DR has lower variance in expectation, not smaller error on every sample,
and IPS happened to land closer. The corrected test averages over 12 independent
reward draws, which is where the claim is actually true.

The refusals: `test_a_policy_the_log_never_covers_is_not_identified`,
`test_a_thinly_explored_target_is_flagged_as_concentrated`,
`test_a_single_dominant_row_is_reported`,
`test_clipping_is_counted_and_its_direction_stated`, and the four
`should_deploy` outcomes.
