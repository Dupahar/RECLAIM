# ADR-0016 — Persisting the same run twice is a no-op

**Status:** Accepted · 2026-08-28
**Context phase:** post-Sprint-1 review

## Context

`LedgerRepository.save_posting` and `AuditRepository.append_event` appended to
their store unconditionally. Calling `persist_run()` twice with the same report
therefore wrote every record twice.

The ledger absorbed this: `Ledger.post` is idempotent by posting id, so
rehydration produced correct balances from the duplicated file. The audit log did
not — a `MerkleAuditLog` is a *sequence*, so 4 events became 8 and the reloaded
root changed.

The result was the worst possible failure shape: **a legitimate re-run became
indistinguishable from tampering.** The published root no longer matched the
stored one, and `--replay --expect-root` failed on data nobody had attacked.

## Decision

Give both repositories the same content-idempotency rule the in-memory `Ledger`
already has: append a record only if that exact record is not already stored.

- A shared `_DedupingRepository` base snapshots the store's canonical record set
  on first write and keeps it current as it appends — one store read per
  persist, not one per record.
- The dedupe key is the canonical JSON form (`sort_keys`, tight separators) —
  byte-identical to what `JsonlFileStore` writes, so "already stored" means
  exactly what it appears to mean.

`persist_run(report)` twice now leaves the files, the event count, and the root
unchanged.

## Why content and not an id

Audit events have no id — they are `(kind, at, detail)`. Within a run,
`build_audit_log` always distinguishes them by the entity in `detail` (settlement,
leak, or candidate id), so two byte-identical events in one log would mean the
same decision recorded twice, which is not information. Content is the identity.

## Consequences

- The ledger store also stops duplicating lines. Rehydrated state was already
  correct; the file is now honest about what happened.
- Append-only is preserved in the sense that matters: records are never edited
  or removed, and a genuinely *new* record always appends.
- A store directory can now be re-persisted safely, so recovering from a crashed
  run no longer requires deleting the store first.

## Tested by

`tests/test_persistence.py` — `test_audit_repository_append_is_idempotent`,
`test_audit_repository_dedupes_against_pre_existing_store`,
`test_audit_repository_keeps_distinct_events`,
`test_ledger_repository_save_is_idempotent`.
`tests/test_persist_run.py::test_persist_run_twice_is_a_no_op` proves the
end-to-end property: root stable, replay still verifies.
