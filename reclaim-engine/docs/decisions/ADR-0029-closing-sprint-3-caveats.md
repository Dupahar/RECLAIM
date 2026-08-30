# ADR-0029 — Closing Sprint 3's own caveats

**Status:** Accepted · 2026-08-31 · finishes work flagged incomplete in [ADR-0022](ADR-0022-observation-loop.md), [ADR-0023](ADR-0023-hitl-control-plane.md), [ADR-0024](ADR-0024-uplift-targeting.md), [ADR-0027](ADR-0027-bandit-and-offline-evaluation.md), [ADR-0028](ADR-0028-conduct-guardrails.md)

## Context

Sprint 3 shipped six phases and each one's ADR ended with an honest list of what
it had not done. Several of those items were not deferred *features* — they were
gaps that undermined the claim the phase had just made. Left alone they would
have been the first things a reviewer found, and correctly so.

This ADR records closing six of them.

## Decisions

### 1. A contact cap now survives a restart

ADR-0028's entire argument is that *"a cap that lives in memory is not a cap"*,
and it then shipped `ConsentRegistry` and `ContactLedger` in memory. The cap
survived a re-run within a process and reset silently on restart, which is the
exact failure the ADR was written about.

`ConsentRepository` and `ContactRepository` follow the existing
`_DedupingRepository` pattern. Consent is stored per grant so a reloaded
registry still answers `state_at` for a *past* timestamp — the property that
makes a replay auditable rather than merely repeatable. Contacts stay idempotent
by `idempotency_key` through both re-persist and reload, so neither can consume
a customer's allowance twice.

### 2. A leak knows whose money it is

`customer_ref` on `LeakRecord`, carried from the settlement's `counterparty` by
reconciliation. One missing field was blocking three separate capabilities:
contact caps were per-*leak* rather than per-customer, funded-moment history had
no key, and prior-failure counts could not be assembled.

`ConductGate` now defaults to the leak's own `customer_ref`, so caps are
per-customer with no wiring. Where a source carried no counterparty it falls
back to the leak id — a weaker scope, reported by `is_per_customer` rather than
passing for the real thing. An empty-string `customer_ref` is refused, because
it would pool every anonymous leak into one "customer" and share a contact
allowance between strangers.

### 3. The two modules that disagreed about `confidence` now agree

`domain._validate_confidence` rejected `Decimal` while `resolver.Assessment`
rejected `float`. Carried since Sprint 2, listed as unresolved three times. The
consequence was real rather than cosmetic: a probabilistic score or a resolver
confidence — both `Decimal` — could not be carried into a `LeakRecord` without a
lossy conversion first. Widened rather than narrowed, so no existing caller
changes. Non-finite `Decimal` values still fail the range check without needing
their own guard, since `float(Decimal("NaN"))` is `nan` and `0.0 <= nan <= 1.0`
is False.

### 4. The bandit is actually called

ADR-0027 shipped the policy and the evaluators and admitted *"nothing in the
recovery engine calls the bandit yet"*. An `ActionPolicy` seam now lets a policy
choose the channel and message per attempt, and `RecoveryAttempt` records the
`action_key`, `propensity` and `context_key` — the decision record an offline
estimator needs, written at decision time because a propensity reconstructed
afterwards is a different number.

`observation.logged_decisions` bridges a run and its T+1 outcomes into
`LoggedDecision` rows. The reward is the **observed** outcome, never the
executor's result: an attempt whose `AttemptResult` was `SUCCEEDED` proves an API
call returned, and evaluating a policy on that would rank whichever action the
rail happens to accept — a fact about the gateway, not about customers. Only
the first attempt per unit is logged, because later attempts in a sequence are
not independent draws.

**Timing stays with `AttemptScheduler`.** Two components bidding for *when*
would put the notice-window refusal in only one of them. The consequence is
stated in the protocol docstring rather than left to be discovered: a bandit
whose actions differ only by `hour` learns nothing through this seam, because
those arms are the same intervention wearing different keys.

**A bug this surfaced.** The first bridge built the log's action key from the
channel plus the *attempt's* hour, which produced a key matching no action the
policy knew about — so every offline estimate came back "not identified". The
policy's own key is now stored verbatim.

### 5. A gate can prove what it was asked about

ADR-0023 noted `Gate.audit_ref` *"exists and is persisted, but nothing populates
it, so a decision is not yet provable against the audit root the way a leak is"*.

A gate exists *because of* a logged decision — an escalation, a halt, a residual
leak — and those events are already in the Merkle log, so a gate can point at
the leaf that caused it without inventing new event types. `gates_for_run(...,
audit=...)` stamps `"<root>:<leaf index>"`, the pair needed to pull an inclusion
proof, and the CLI passes its audit log through. A gate whose subject has no
event is left unstamped rather than pointed at an unrelated leaf.

### 6. A deployed policy is watched, not just validated once

§9.2 asks for drift monitors and ADR-0027 recorded having none. A policy
validated at deployment and never re-examined decays silently: last month's
right answer and this month's wrong one produce identical logs.

`drift` reports two signals separately, because they fail in that order.
**Reward drift** is what matters and moves last, needing enough failures to clear
the noise. **Action-mix drift** — total-variation distance over chosen actions —
moves first, because a policy that collapses onto one arm changes its mix well
before its reward becomes distinguishable. Kept separate because a mix shift with
stable reward (a new equilibrium) is a different situation from one with falling
reward (thrashing).

The statistics are deliberately plain: a pooled two-proportion z-test and an L1
distance, both in `Decimal`, both checkable by hand. A window below `min_n`
yields `INSUFFICIENT_DATA` rather than a verdict — a monitor that fires on twelve
observations gets muted, after which its silence is uninformative too. `scan`
compares every window to the **first**, because a slow decay would pass a
previous-window comparison every time while the policy quietly halved.

## What is still not done

- **Nothing persists conduct state from a demo or the CLI.** The repositories
  exist and are tested against JSONL on disk; no shipped command writes them,
  because no shipped command contacts anyone.
- **`Context.prior_failures` is still unsourced.** `customer_ref` makes it
  *possible* to assemble a per-customer failure history from the Leak Ledger;
  nothing does yet.
- **No funded-moment feed.** Same shape: the key now exists, the feed does not.
- **The drift monitor is not scheduled.** It is a function, not a job; nothing
  calls it on a cadence or alerts on its verdict.
- **The bandit's reward is binary.** `logged_decisions` emits 0/1 recovered,
  discarding amount. A policy optimising recovered *value* would need a
  reward the estimators' variance diagnostics were not calibrated for.

## Tested by

29 new tests; suite at **783 passing, 100% line + branch**. The ones carrying
the argument: `test_a_contact_cap_survives_a_restart` (spend the allowance in one
process, refuse in another), `test_a_reloaded_registry_still_answers_about_the_past`,
`test_the_ips_identity_holds_on_a_log_the_engine_produced`,
`test_a_better_policy_is_learned_and_then_graded_before_deployment`,
`test_logged_decisions_reward_comes_from_the_bank_not_the_executor` (the executor
always succeeds, so every row would be 1 if rewards were its results),
`test_a_gate_is_stamped_with_the_event_that_caused_it` (the ref resolves to a
leaf whose inclusion proof verifies),
`test_a_collapsed_action_mix_is_flagged_even_when_reward_holds`, and
`test_scan_compares_every_window_to_the_first`.
