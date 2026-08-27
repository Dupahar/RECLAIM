# ADR-0020 — The pre-debit notice is executed, and the AFA ceiling is enforced

**Status:** Accepted · 2026-08-28 · extends [ADR-0004](ADR-0004-bounded-recovery.md)

## Context

Two compliance claims were represented in the engine without being enforced.

**The RBI 24-hour pre-debit notice** was a recorded timestamp. `notice_at` was
set, attempts were scheduled after the window, and nothing ever sent a notice.
A reviewer reasonably read the code as claiming more than it did.

**The RBI e-mandate AFA ceiling** (₹15,000 without Additional Factor
Authentication) appeared in the architecture's compliance table and nowhere in
the code. The engine would happily schedule a ₹90,000 autonomous debit.

## Decision

**A `NoticeExecutor` seam**, mirroring `RecoveryExecutor`. When one is supplied,
the notice is dispatched before any attempt and the retry window is anchored to
a notice that really went out. A notice that is rejected, or a notice channel
that raises, **halts the recovery with no attempts made** — no debit without
notice. The idempotency key is `"<leak id>:notice"`, so one leak means one
notice however often the workflow is retried.

`RecoveryOutcome.notice_sent` records whether dispatch was confirmed. Without a
`NoticeExecutor` it stays `False` and the scorecard's `notice_compliance` reads
0 — the window is modelled, and the engine says so rather than implying it was
served.

**`RecoveryConfig.afa_limit`**, defaulting to ₹15,000. Above it the only correct
autonomous behaviour is to stop: authentication is a customer action the engine
cannot perform, so the leak is `HALTED` for a human with no attempts (G8). The
check is skipped when the leak's currency differs from the ceiling's, because
inventing an exchange rate to enforce a compliance limit would be worse than not
enforcing it.

## Consequences

- Existing callers are unaffected: both features are opt-in-shaped (the notice
  executor defaults to `None`, and the ceiling only bites above ₹15,000 INR).
- `notice_compliance` gives the scorecard a conduct metric that cannot be gamed
  by chasing harder.
- Still missing: contact caps *across* runs, and consent state. Both need the
  cross-run recovery state the Leak Ledger now makes possible.

## Tested by

`tests/test_recovery.py` — notice dispatched before the debit at t0 with the
attempt at t+24h; rejected notice and failed notice channel both halt with zero
attempts; amounts above / at / below the ceiling; ceiling disabled, retuned, and
ignored across currencies.
