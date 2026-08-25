# ADR-0009 — Replay verification & tamper detection via a published root

**Status:** Accepted · **Date:** 2026-08-26

## Context
Persistence (ADR-0008) stores an append-only ledger + audit log. We need a way
to *prove*, at any later time, that a stored run has not been altered — and to
surface it from the CLI.

## Decision
Add `verification.verify_stores(ledger_store, audit_store, expect_root=None)`
returning a `VerificationResult`, and a CLI `--replay DIR [--expect-root HEX]`.

Verification re-checks, from scratch:
1. **Ledger balances** — rehydrate and confirm `debits == credits` per currency.
2. **Inclusion proofs** — every audit event verifies under the recomputed root.
3. **Published-root match (optional)** — if `--expect-root` is given, the
   recomputed audit root must equal it. This is the primary tamper-detection
   mechanism: publish the root once (e.g. print it at `--store` time / record it
   elsewhere), then replay later and confirm it is unchanged.

**Rehydration is itself a gate.** A tampered ledger record that no longer
balances, a conflicting duplicate posting id, or corrupt JSON raises during
load; the CLI catches these and reports `FAILED` (exit 1). Any edit/append to
the audit log changes the root, so `--expect-root` fails.

## Consequences
- Exit codes: `0` verified, `1` verification failed (unbalanced / proof fail /
  root mismatch / unrehydratable), `2` usage error (no run found / neither batch
  nor `--replay`). The `batch` positional became optional to accommodate replay
  mode.
- Without `--expect-root`, verification confirms *internal* consistency (balanced
  + proofs) but cannot detect a wholesale rewrite of both files — hence the
  published-root check is the real anti-tamper guarantee. This limitation is
  stated, not hidden.
- Empty stores verify vacuously (balanced, no proofs) — a "no run" guard in the
  CLI prevents mistaking an empty/missing directory for a verified run.

## Alternatives considered
- **Signing each record** — stronger (authenticity, not just integrity) but adds
  key management; deferred. The Merkle root + publication is sufficient for
  tamper-evidence at this stage.
- **Storing the root inside the same directory and trusting it** — rejected: a
  co-located root can be rewritten by the same tamperer; the root must be
  published/held out of band for the check to mean anything.
