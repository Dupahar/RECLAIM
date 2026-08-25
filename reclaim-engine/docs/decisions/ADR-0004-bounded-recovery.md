# ADR-0004 — Recovery is bounded, compliant, and deterministic by construction

**Status:** Accepted · **Date:** 2026-08-25

## Context
The recovery engine acts on money (retrying debits, nudging customers). This is
the most sensitive part of RECLAIM: done wrong it double-charges or harasses.
We must decide how autonomy, compliance, and testability are handled.

## Decision
1. **Deterministic orchestration.** `recover(leak, reason, base_time)` is a pure
   function of its inputs plus a deterministic executor — no `now()` inside;
   the caller supplies `base_time`. Reproducible and replayable (G4).
2. **Compliance encoded as hard rules, not guidance.** The first debit attempt is
   scheduled only after the RBI-mandated **24-hour pre-debit notice**
   (`notice_hours`); attempts are **capped** (`max_attempts`); attempts are
   spaced (`gap_hours`). These are structural, not advisory.
3. **Idempotency for money actions (G5).** Each attempt carries a deterministic
   key `"{leak.id}:attempt:{k}"` so a retried workflow never double-debits. The
   real executor must honour the key end-to-end.
4. **External effect behind an interface.** A real UPI/gateway/WhatsApp action
   implements `RecoveryExecutor`; tests use deterministic fakes
   (`AlwaysSucceeds`/`AlwaysFails`/`Sequence`/`Raising`). The engine logic is
   therefore 100%-testable offline.
5. **Safe degradation (G6).** A **permanent** cause (mandate revoked / hard
   decline) is *not chased* → `NOT_RECOVERABLE` (no attempts, no harassment). An
   **unknown** cause or an **executor error** → `HALTED` (escalate to human). The
   engine never guesses.
6. **Auditable.** Every outcome carries the full ordered attempt log and the
   notice timestamp.

## Consequences
- `RecoveryState.HALTED` was added to the domain (additive) to represent
  "stopped mid-recovery, needs a human."
- The engine currently retries on a fixed schedule with channel rotation. The
  ML pieces from the architecture — **funded-moment prediction** and a
  **contextual bandit** over (timing × channel × message) — are future
  `RecoveryExecutor`/planner strategies that slot in without changing the
  bounded/compliant/auditable skeleton.
- Conservative by design: it will stop and escalate rather than over-attempt.

## Alternatives considered
- **Let the engine call `now()` and schedule freely** — rejected (non-deterministic,
  un-replayable, and easy to violate the notice window).
- **Best-effort retries without idempotency keys** — rejected (double-debit risk).
- **Chase every failure** — rejected (harassment + wasted effort on dead mandates;
  violates bounded autonomy).
