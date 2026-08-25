# ADR-0007 — Tamper-evident audit via an RFC-6962-style Merkle log

**Status:** Accepted · **Date:** 2026-08-25

## Context
RECLAIM's integrity promise (architecture Section 9) requires that every
decision — matches, recoveries, escalations, and *non*-actions — be recorded
such that any later edit or deletion is detectable. We must choose a design and
keep it testable and dependency-light.

## Decision
Implement an append-only **Merkle transparency log** following the
Certificate-Transparency design (RFC 6962; Crosby-Wallach history tree), using
only stdlib `hashlib`/`json`:

- **Domain-separated hashing:** `leaf = SHA256(0x00 || data)`,
  `node = SHA256(0x01 || left || right)`. The prefixes prevent
  leaf/node-confusion and second-preimage attacks.
- **Merkle Tree Hash (MTH)** commits to the whole ordered log; the root is the
  tamper-evidence anchor.
- **Inclusion proofs:** O(log n) audit paths, generated and verified via the
  same recursion (proof entries carry an explicit left/right direction, making
  verification unambiguously correct and fully testable).
- **Append-only checking:** `root_at(size)` recomputes a prefix root so a
  previously-published root can be re-verified as an unchanged prefix. This is a
  simple, correct consistency check; RFC 6962's O(log n) consistency proof is
  the scale-up.
- **Canonical event serialization:** events serialize with sorted keys, so
  hashing is independent of dict insertion order.
- **Integration:** `pipeline.build_audit_log(report, at)` records every decision
  kind of a run in a fixed order — kept as a separate, composable function so
  the verified pipeline is untouched.

## Consequences
- Editing or deleting any past event changes the root (verified by tests);
  presenting a tampered event fails its inclusion proof.
- Explicit-direction proofs deviate cosmetically from RFC 6962's
  index-derived directions but are an equivalent, valid Merkle inclusion proof —
  chosen so the crypto is transparently correct rather than subtly wrong (a
  wrong verifier would be worse than none — a false sense of integrity).
- `root_at` prefix checking is O(m); acceptable for a reference implementation.

## Alternatives considered
- **A plain append-only list / hash chain** — rejected (no efficient inclusion
  proofs; weaker than a Merkle commitment).
- **Full RFC 6962 O(log n) consistency proofs now** — deferred; the prefix
  recompute is correct and simpler for the foundation.
- **A hand-rolled directionless RFC verifier** — rejected as error-prone;
  chose the explicit-direction proof for provable correctness.
