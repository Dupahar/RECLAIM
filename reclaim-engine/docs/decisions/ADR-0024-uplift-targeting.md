# ADR-0024 — Target on uplift, and keep the model explainable enough to argue with

**Status:** Accepted · 2026-08-31 · implements architecture §7 (uplift model)

## Context

The recovery engine chased every recoverable leak. That is defensible as a
starting point and indefensible as a product, because it conflates four
different customers:

| segment | recovers if chased | recovers if left alone | chase? |
|---|---|---|---|
| sure thing   | yes | yes | **no** — the money was coming anyway |
| persuadable  | yes | no  | **yes** — this is the whole product |
| lost cause   | no  | no  | **no** — contact with no upside |
| sleeping dog | no  | yes | **never** — chasing makes it worse |

A response model ("who will recover?") cannot separate these, because three of
the four rows contain recoveries. Chasing sure things inflates gross recovery
while adding nothing — precisely the number [ADR-0022](ADR-0022-observation-loop.md)
exists to stop us reporting — and chasing sleeping dogs destroys value while
looking like activity.

Phase 24 made this newly buildable: the T+1 loop produces *observed* outcomes
labelled by arm, which is exactly the training set an uplift model needs and
which did not exist before.

## Decision

A `uplift` module — a **T-learner over discrete cells** — plus an optional
`Targeting` parameter on `run_reclaim`.

**Discrete cells, not gradient boosting.** Contexts bucket into
`failure reason × amount band × recency band × prior-failure band`; each cell
holds treated and control counts; uplift is the difference of the two rates.
Two consequences were the reason for the choice: every prediction is explainable
down to the counts behind it, and the model is deterministic, so a run replays
(G4). A gradient-boosted learner on continuous features would fit better with
enough data and would be neither explainable to a merchant nor trainable on the
volume a design partner has in month one. Revisit when a partner has a year of
labelled outcomes.

**Trained on observed outcomes only.** `TrainingRow.recovered` must be the T+1
loop's verdict, not the executor's claim. Training on claims would teach the
model that whatever the executor reports success for is "persuadable", which is
not a fact about customers at all.

**Hierarchical fallback, because low volume is the normal case.** A thin cell
falls back to the pooled estimate for its failure reason, then to the global
estimate, then to `INSUFFICIENT_EVIDENCE` — and every estimate names the level
that answered. Support is required in *both* arms: an uplift needs a difference
to be a difference. This answers the architecture's own open question ("can lift
be estimated reliably for small merchants? may need hierarchical/pooled
models") in code rather than in prose.

**Classification order is deliberate.** Sleeping dogs are checked first, so a
segment that is actively harmed is never downgraded to merely unprofitable.
Then sure things (a high do-nothing rate makes the contact pointless whatever
the treated rate looks like), then lost causes, then persuadable — so a unit is
only chased when nothing else explains it. A real but sub-threshold uplift is
reported as a lost cause rather than rounded up.

**Thresholds are configuration, not literals.** `SegmentThresholds` is an
explicit business choice about how much incremental value justifies a contact.

**The model proposes; the caller decides.** `UnknownPolicy` defaults to `CHASE`,
preserving today's behaviour where the model has no reliable estimate. This is
the deliberate direction to fail: a model quietly shrinking recovery coverage on
thin evidence loses a merchant money they were owed, which is worse than
chasing a few sure things. `SKIP` is available for a deployment that disagrees.

**Skipping is reported, never netted away.** `RunReport.skipped_leaks` carries
each skipped leak *with its reason*, and `skipped_amount()` totals the value the
engine chose to leave. A targeting layer that hides that number is unauditable —
and a skipped leak stays on the honest residual list, because it did not stop
being missing money.

**The holdout is decided before targeting.** A control unit that targeting would
also have skipped still counts as held out. Otherwise the experiment stops
measuring the policy that was actually deployed.

## What this deliberately does not do

`Context.prior_failures` is the one feature the engine cannot currently source
on its own — nothing tracks a customer's failure history across runs yet. It is
a required field rather than a defaulted one so that gap is visible at every
call site instead of silently reading zero.

There is no propensity logging and no offline policy evaluation, so a *new*
targeting policy cannot yet be validated against logged data before deployment.
That is the bandit's prerequisite and belongs with it.

Nothing retrains automatically. Fitting is an explicit call, which is the right
default for a model that decides whether to debit someone.

## Tested by

`tests/test_uplift.py` (34) and 8 in `tests/test_pipeline.py`. The four that
carry the argument are the segment recoveries —
`test_a_sure_thing_is_not_chased_despite_a_high_treated_rate` (90% recover when
chased, 85% when left alone: a response model ranks this top and uplift
correctly declines), `test_a_sleeping_dog_is_never_chased`,
`test_a_sleeping_dog_outranks_a_sure_thing_label`, and
`test_a_real_but_tiny_uplift_is_not_rounded_up_to_persuadable` — alongside
`test_a_one_armed_cell_never_produces_an_uplift`,
`test_with_no_evidence_the_model_says_so`, and
`test_the_control_arm_is_decided_before_targeting`.
