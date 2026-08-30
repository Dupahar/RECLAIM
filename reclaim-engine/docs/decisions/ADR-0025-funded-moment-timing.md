# ADR-0025 — Retry when the account is funded, and never before the notice window closes

**Status:** Accepted · 2026-08-31 · implements architecture §7 (funded-moment predictor)

## Context

The bounded engine retried on a fixed schedule: notice + 24h, then every
`gap_hours`. That schedule is indifferent to the single fact that decides whether
a debit succeeds — whether there is money in the account at that moment.

In India that fact is strongly cyclical. Salary credits cluster in the first week
of the month, so a retry on the 28th against an account funded on the 1st is a
contact spent on a near-certain decline: it burns a contact against the caps in
[ADR-0004](ADR-0004-bounded-recovery.md), moves `wasted_contact_rate` the wrong
way, and teaches the customer that the notices mean nothing. This is the Stripe
Smart Retries insight the architecture cites, applied to UPI Autopay.

## Decision

A `timing` module and an `AttemptScheduler` seam on `RecoveryEngine`.

**A concentration model, not a classifier.** For a customer, count the
day-of-month and hour-of-day of every moment money was demonstrably there, take
the mode of each, and report *how concentrated* the history was as the
confidence. It is a real signal, it explains itself to a merchant in one sentence
("you get paid on the 2nd"), and it needs a dozen observations rather than a
billion. A production version would condition on more than the calendar, at the
cost of both properties.

**Confidence is concentration, and it is allowed to be low.** A customer whose
funding is spread evenly across the month gets a prediction whose confidence
says so, plus a note recommending the fixed schedule. A timing model that cannot
see a pattern must not pretend to.

**Hierarchical fallback, as in [ADR-0024](ADR-0024-uplift-targeting.md).** Thin
customer history falls back to a pooled prior across all customers, then to
nothing, and a pooled prediction says explicitly that it is not a personal one.

**Three failure modes, three distinct responses.** `schedule_attempt` only
prefers the prediction when it is *known*, *confident*, and lands after the
deadline. Collapsing those into one boolean would hide which of "no data",
"diffuse data" and "illegal timing" occurred, and they call for different fixes.

**The compliance window is not the model's to overrule.** This is the load-bearing
decision. The scheduler is *advisory*: the engine hands it the notice deadline and
its own fallback, and anything returned at or before the deadline **halts the
recovery** rather than being obeyed. A non-datetime halts too. A timing model that
could shorten the RBI notice window would be a compliance bug wearing an ML hat,
so the refusal lives in the engine, not in the model's good behaviour.
`next_after` independently guarantees a time strictly after its bound, and a
day-of-month that does not exist in the target month (the 31st of September)
clamps to that month's last day rather than silently skipping a cycle.

**Re-timing preserves spacing.** Only the first attempt moves; later attempts
space from wherever it landed. Otherwise a re-timed sequence would bunch back
against the original fixed schedule and deliver three attempts in a day.

## What this deliberately does not do

Nothing in the engine yet *sources* funded moments. The predictor is fitted from
a caller-supplied history, and the events that would populate it — successful
collections, incoming credits per customer — are not currently associated with a
customer identity anywhere in the domain. That is the honest gap: the seam and
the model are real and tested; the feed is not built.

There is no timezone model. Predictions inherit the tzinfo of the bound they are
placed against, which is correct arithmetic but not a considered position on
which clock a merchant's customers live in.

The hour prediction is computed and reported but does not carry its own
confidence gate — only `day_confidence` decides whether to re-time. Hour-level
signal is much noisier and gating on it separately was not worth the branch
until there is real data to calibrate against.

## Tested by

`tests/test_timing.py` (28) and 6 in `tests/test_recovery.py`. The two that
matter most are `test_a_scheduler_cannot_shorten_the_notice_window` (three
different illegal proposals, all halted with zero attempts) and
`test_a_retimed_attempt_never_lands_inside_the_notice_window`, which sweeps 28
notice deadlines and asserts the scheduled time is after every one. Then
`test_the_mode_wins_not_the_average` (the average of the 1st and the 29th is the
15th, when the account is empty on the 15th),
`test_a_day_that_does_not_exist_clamps_instead_of_skipping_a_cycle`,
`test_a_diffuse_history_says_so_rather_than_guessing_confidently`, and
`test_the_real_timing_module_drops_into_the_seam`, which runs the actual
predictor through the engine rather than a lambda.
