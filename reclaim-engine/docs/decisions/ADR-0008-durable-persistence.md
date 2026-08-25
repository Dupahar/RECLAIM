# ADR-0008 — Durable persistence via event sourcing (append-only)

**Status:** Accepted · **Date:** 2026-08-26

## Context
The ledger and audit log must survive process restarts. Both are already
append-only and deterministically replayable, so we must choose a storage model
that preserves those properties (immutability, replay determinism) rather than
fighting them.

## Decision
Use **event sourcing**: persist an append-only stream of records and rehydrate
state by replaying them.

- **One interface, two stores.** `EventStore` protocol with `append` / `read`;
  `InMemoryStore` (tests) and `JsonlFileStore` (durable JSON Lines on disk, one
  record per line, file only ever appended to).
- **Repositories replay to identical state.** `LedgerRepository.load()` posts
  every stored `Posting` into a fresh `Ledger` (same balances); 
  `AuditRepository.load()` appends every stored `AuditEvent` into a fresh
  `MerkleAuditLog` (**same Merkle root**). Verified by tests.
- **Exact-value serialization.** `Money` is stored as `{amount: str, currency}`
  (never a float), preserving full decimal precision through a round trip.
- **Malformed data fails loudly.** Corrupt JSON, missing fields, or invalid
  money raise a located `PersistenceError`; a stored pair of conflicting records
  with the same posting id is caught on replay by the ledger's idempotency guard
  (`DuplicatePostingError`) — persistence inherits the domain's integrity checks.
- **InMemoryStore copies on write/read** so callers cannot mutate history.

## Consequences
- Corrections are new records, never edits (G3) — the JSONL file is append-only,
  matching the ledger/audit immutability model.
- Replay is O(n); fine for the foundation. Snapshotting/compaction is a future
  optimization.
- The store is deliberately format-simple (JSONL); swapping in SQLite or an
  event-store service later is behind the `EventStore` interface, no domain
  change.

## Alternatives considered
- **Mutable row storage (update-in-place)** — rejected (breaks immutability and
  auditability; the whole point is an append-only history).
- **SQLite now** — deferred; JSONL is dependency-free, human-inspectable, and
  sufficient for the foundation. Interface allows a later swap.
- **Pickle** — rejected (opaque, unsafe, non-portable, hides the no-float rule).
