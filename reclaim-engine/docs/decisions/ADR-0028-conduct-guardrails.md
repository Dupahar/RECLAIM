# ADR-0028 — A cap that lives in memory is not a cap

**Status:** Accepted · 2026-08-31 · completes architecture §7's hard stopping rules (contact caps, consent)

## Context

The architecture lists three hard stopping rules: *"contact caps, RBI 24h notice,
consent"*, enforced *"in the workflow, not left to a model"*. Two were enforced.

**The contact cap was enforced in the wrong scope, and the failure is
embarrassing when stated plainly.** `max_attempts` caps attempts *within a
single recovery run*. Nothing capped contacts *across* runs. Re-run yesterday's
batch today and the same customer is contacted again, three more times, and the
engine cannot see that it has done so. A daily cron would have produced textbook
harassment while every in-process invariant held and every test passed. A cap has
to live in durable state or it is not a cap.

**Consent was not modelled at all**, which is worse, because the failure is
silent: an engine that has never heard of consent behaves identically to one
whose customers have all granted it. Nothing anywhere would have looked wrong.

## Decision

A `conduct` module holding the durable state and the rules, and a `ConductRule`
seam on `RecoveryEngine` alongside the notice and scheduler seams.

    consent -> quiet hours -> cooling-off -> window cap -> allowed

**Default deny.** An unknown consent state refuses. This is the single most
consequential line in the module: "we have no record" and "they said yes" must
not produce the same behaviour, and the direction of the default decides which
one a missing row silently becomes. With `require_consent` on and *no registry
supplied at all*, every contact is refused rather than waved through — so
adopting the gate incrementally fails closed, not open.

**Consent is read as of a timestamp, not as it stands now.** The registry is
append-only and `state_at(customer, when)` scans to that moment. A replay of last
week's run therefore sees last week's consent. A guardrail that re-decides
history with today's data cannot be used to audit what was actually allowed at
the time, which is the only question an auditor asks.

**Replay must not consume the allowance.** Contacts are idempotent by
`idempotency_key` — the same key the payment attempt already carries — so
re-persisting or replaying a run neither double-counts a contact nor exhausts a
customer's cap. This is the rule the money ledger, the audit log and the Leak
Ledger already apply, extended to the one piece of state that lacked it.

**Rule order is deliberate.** Consent first, because it is the only rule whose
violation is a wrong done to a person rather than a policy breach: no cap or
quiet-hour argument makes contacting someone who withdrew acceptable. Quiet hours
next, because a contact at 3am is harmful whatever the counts say. Then the
cooling-off period, then the window cap, which is the narrowest. A test asserts
consent outranks quiet hours when both would refuse, so the reported rule is the
one a customer would care about.

**Consulted before every attempt, not just the first.** A cap can be reached
part-way through a sequence and a consent withdrawal mid-sequence must stop the
next debit. Two tests cover exactly those.

**A refusal halts, and names the rule.** `RecoveryState.HALTED` with the rule id
in the rationale, which is the shape the Phase 25 HITL queue already renders — so
"we are not allowed to contact this person" becomes a question a human sees
rather than a silent non-event.

**The gate authorises but does not record.** `note()` is a separate call the
caller makes after a contact actually happens. Something that both authorised and
recorded would make a dry run indistinguishable from a real one, and would
consume a customer's allowance every time anyone asked a hypothetical.

**Defaults are conservative** — 3 contacts per 30 days, 24h cooling-off, quiet
hours 21:00–08:00 — because a recovery product's failure mode is not "too few
contacts"; it is a merchant's customer receiving a fourth debit notice in a week
and never using that merchant again.

## What this deliberately does not do

**Nothing persists the conduct state to disk yet.** `ConsentRegistry` and
`ContactLedger` are in-memory, mirroring `LeakLedger` before Phase 19 gave it a
repository. The whole argument of this ADR is that a cap must be durable, so
until a `ConductRepository` exists the cap survives a *re-run* within a process
but not a restart. That is the gap to close next and it is the reason this ADR
does not claim the rule is fully enforced in production terms.

**`ConductGate.customer_for` defaults to the leak's own id**, which makes caps
per-leak rather than per-customer — almost never what a deployment wants. The
domain has no customer identity on a `LeakRecord`, so there is nothing better to
default to; the docstring and a test say so explicitly rather than letting it
look correct.

**Quiet hours are naive local hours.** No timezone model, no per-customer
locale. For an India-first product a single clock is defensible; for anything
else it is wrong, and it is not parameterised.

Nothing yet feeds real consent events in. There is no Account Aggregator or
mandate-registry integration, so in every demo the registry is populated by the
caller.

## Tested by

`tests/test_conduct.py` (36) and 6 in `tests/test_recovery.py`. The load-bearing
ones are the cross-run tests, because that is the gap:
`test_the_window_cap_binds_across_runs` (three contacts across three separate
runs exhaust the allowance and the fourth is refused),
`test_replaying_a_contact_does_not_consume_the_allowance`,
`test_a_cap_reached_part_way_through_stops_the_next_attempt`, and
`test_a_consent_withdrawal_mid_sequence_stops_the_next_debit`. Then default-deny:
`test_with_no_consent_registry_at_all_every_contact_is_refused`,
`test_without_consent_no_debit_is_attempted`, and
`test_unknown_is_the_absence_of_a_record_not_a_record`. And
`test_consent_is_read_as_of_the_moment_not_as_it_stands_now`, which is what makes
the module auditable rather than merely restrictive.
