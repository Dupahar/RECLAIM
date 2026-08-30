# ADR-0026 — Demonstrate the loop closing, and measure the policy rather than the subset it chose

**Status:** Accepted · 2026-08-31 · demonstrates [ADR-0022](ADR-0022-observation-loop.md) + [ADR-0024](ADR-0024-uplift-targeting.md); `python -m reclaim.cycles`

## Context

Phase 26 shipped an uplift model and a funded-moment predictor with 76 tests and
100% branch coverage, and nothing used them end to end. That is precisely the
criticism Sprint 2 earned for causal measurement — *"cohorts were assigned, but
nobody ever went back to see what happened to them"* — reappearing one layer up
as "the model is fitted, but nobody ever targets with it". A capability that is
only reachable from a unit test is not a capability a buyer can evaluate.

The loop the architecture describes has four steps and the engine could only
show three: detect → recover → **observe** → **learn** → target. Steps three and
four existed separately.

## Decision

A second demo command, `reclaim.cycles`, running two full periods:

    cycle 1: chase everyone       -> observe at T+1 -> fit an uplift model
    cycle 2: chase the persuadable -> observe at T+1 -> compare

**A separate command, not a change to `reclaim.experiment`.** The single-cycle
demo's numbers are quoted verbatim in ADR-0022, the build log and the runbook.
Rewriting its cohort would have silently invalidated all three. `experiment`
answers "does the measurement work"; `cycles` answers "is the measurement worth
having". Those are different questions and deserve different fixtures.

**Three authored behaviours, chosen so the naive policy loses.**

| behaviour | shortfall | recovers if contacted | recovers if left alone |
|---|---|---|---|
| sure thing   | ₹200   | 90% | 85% |
| persuadable  | ₹1,500 | 65% | 25% |
| sleeping dog | ₹8,000 | 25% | 55% |

The sleeping dogs hold the largest amounts, which is the realistic and
uncomfortable case: the units most worth chasing by value are the ones chasing
harms. A response model ranks the sure things first, because 90% is the highest
recovery rate on the sheet.

**The fixture's outcome depends on *contact*, not on arm.** A treated unit that
targeting declined to contact behaves like an untouched one. Keying the outcome
off the arm label instead would have made targeting look free — the skipped
units would still have "recovered at the treated rate" — which would be a demo
that cannot fail.

**Lift is measured over the whole policy, not the contacted subset.** This is
the load-bearing statistical decision. Both arms retain all three behaviours in
both cycles: *treated* means "the policy ran", which in cycle 2 legitimately
includes units the policy chose not to contact. Restricting the treated arm to
contacted units while leaving the control arm whole would compare two different
populations and inflate cycle 2's lift by construction. This is the same reason
`run_reclaim` assigns cohorts **before** targeting (ADR-0024), and a test pins
that both arms keep the full behaviour mix.

**The cohort is 900, not 210, and the reason is a real finding.** At 210 the
control cells held 16–20 units, the sleeping-dog cell fell below `min_support`,
and the hierarchical fallback answered from the pool instead — which, with a
single failure reason in the fixture, pools *across behaviours* and returned
`persuadable` for the one segment that must never be chased. The model was
behaving exactly as designed and the design was wrong for this shape of data.
Rather than lower `min_support` to make the demo pass, the cohort was sized so
every cell reads its own counts, and a test asserts every estimate's basis is
`BASIS_CELL`. The honest lesson — that pooling by failure reason is nearly
useless when there is only one failure reason — is recorded here rather than
smoothed over.

**Per-cycle salts.** Cohort assignment, cycle 1's outcomes and cycle 2's
outcomes draw from three different salts, so arm and outcome are independent and
the two periods are genuinely different months rather than the same month twice.
Tests pin both properties, because a shared salt would manufacture a lift out of
correlated hashes.

## What the demo shows

| | cycle 1 (chase everyone) | cycle 2 (targeted) |
|---|---|---|
| units contacted | 612 | 204 |
| measured lift | 4.72 pp | 18.38 pp |
| claimed recovered | ₹19,86,600 | ₹3,06,000 |
| observed recovered | ₹6,29,300 | ₹11,64,400 |
| **causal recovered** | **−₹5,08,425.82** | **+₹1,72,093.30** |
| causal per contact | −₹830.76 | +₹843.59 |

Chasing everyone claimed nearly ₹20 lakh recovered while destroying ₹5 lakh of
value. The model — told nothing about which shortfall means which behaviour —
read all three out of one period of observed bank outcomes and produced the
table in the demo output, which is the auditable artifact the discrete-cell
choice in ADR-0024 was bought for.

## What this deliberately does not do

The batches and the three behaviour profiles are **synthetic**, so the *size* of
the improvement is a property of the fixture. The direction is not: given those
behaviours, skipping them is arithmetically better, and the code finds which to
skip unaided. Nothing here is evidence about real Indian payment behaviour.

Only two cycles run. There is no drift detection, no retraining cadence, and no
guard against a model that was right last month and wrong this one — a targeting
policy that degrades silently is the obvious next risk and is not addressed.

Cycle 2's policy is evaluated *after* deployment, by running it. Validating a
candidate policy against cycle 1's logged data *before* exposing customers to it
needs propensity logging and IPS/DR estimators, which do not exist yet.

## Tested by

`tests/test_cycles.py` (24). The claim is attacked from both sides: that the
model really discovered the behaviours (`test_the_model_discovers_all_three_
behaviours_unaided`, `test_every_learned_estimate_comes_from_its_own_cell`,
`test_training_labels_come_from_observed_outcomes_not_claims` — the executor
always succeeds, so every treated label would be `True` if labels were claims),
and that the improvement is not an artefact
(`test_both_arms_still_contain_every_behaviour_in_cycle_two`,
`test_the_outcome_draw_is_independent_of_cohort_assignment`,
`test_the_two_cycles_draw_independently`). Plus
`test_chasing_everyone_destroys_value_and_targeting_reverses_it` and
`test_a_skipped_leak_stays_on_the_honest_residual_list` — declining to chase
money does not make it stop being missing.
