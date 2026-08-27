# ADR-0018 — The Leak Ledger is append-only and versioned

**Status:** Accepted · 2026-08-28 · implements architecture §5.2

## Context

Architecture §5.2 calls the Leak Ledger "the integration contract between
reconciliation (writer) and recovery (reader/writer), and the object the human
exception queue renders."

It did not exist. `LeakRecord` was a type, and a run's leaks lived only inside an
in-memory `RunReport` that was discarded at process exit. Three things were
therefore impossible: a durable exception queue, recovery state surviving a
restart, and answering "what is still open?".

## Decision

A first-class `leak_ledger` module, sitting alongside `ledger` exactly as the
architecture pairs the money ledger with the leak ledger, plus a
`LeakRepository` for durability alongside the existing repositories.

**A leak's state changes are new versions, never edits.** A leak moves from
detected to recovered / exhausted / halted over its life. Editing the record to
reflect that would break G3 for the same reason editing a posting would, so
`transition()` appends a new version and `history()` keeps every one. "Why is
this leak in this state?" is always answerable.

**A fourth outcome had to be named.** A leak that a later fuzzy or AI match
resolves was never missing money — it was a matching failure. Calling it
`RECOVERED` would inflate recovery; leaving it open would put a non-problem in
someone's queue. `RecoveryState.SUPERSEDED` says what actually happened.

**`audit_ref` is a real reference.** Appendix B lists it; rather than add an
unused field, `build_leak_ledger` stamps each leak with `"<root>:<leaf index>"`
— exactly the pair needed to pull an inclusion proof — so a leak can prove its
own place in the audit log.

## Consequences

- `open_queue()` is provably the same set as the report's honest residual, which
  is asserted as a test. Two representations of "what a human owns" that could
  drift are worth catching.
- `--store` writes `leaks.jsonl`; re-persisting is a no-op (ADR-0016 applies).
- `evidence[]` was added to `Transaction` and `LeakRecord`, completing
  Appendix B.
- Cross-run recovery state is now possible but not yet wired: the engine still
  recovers within a single run. The store is the prerequisite, not the feature.

## Tested by

`tests/test_leak_ledger.py` (16 tests) and the Leak Ledger section of
`tests/test_persistence.py`.
