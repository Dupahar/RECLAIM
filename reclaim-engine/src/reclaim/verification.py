"""Replay verification — re-check the integrity of a stored run.

Loads a persisted ledger + audit log and re-verifies, from scratch, that:
- the ledger is globally balanced (double-entry invariant intact),
- every audit event has a valid inclusion proof under the recomputed root, and
- (optionally) the audit root matches a previously-published root.

The last check is the real tamper-detection story: publish the audit root once,
then at any later time replay the stored files and confirm the root is
unchanged. Any edit/deletion of a stored decision changes the root and fails.

Loading itself is an integrity gate: a tampered ledger record that no longer
balances, or a conflicting duplicate posting id, raises during rehydration and
is reported as a verification failure by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .persistence import AuditRepository, EventStore, LedgerRepository


@dataclass(frozen=True)
class VerificationResult:
    ledger_balanced: bool
    currencies: tuple[str, ...]
    posting_count: int
    audit_events: int
    audit_root: str
    proofs_ok: bool
    root_matches_expected: Optional[bool]  # None if no expected root supplied
    ok: bool


def verify_stores(ledger_store: EventStore, audit_store: EventStore,
                  expect_root: Optional[str] = None) -> VerificationResult:
    """Rehydrate and verify a stored run. Raises on unrehydratable data
    (corruption / broken invariants) — the caller treats that as a failure."""
    ledger = LedgerRepository(ledger_store).load()
    audit = AuditRepository(audit_store).load()

    currencies = tuple(sorted({e.amount.currency for e in ledger.entries()}))
    ledger_balanced = all(ledger.is_globally_balanced(c) for c in currencies)  # vacuously True if empty

    root = audit.root()
    proofs_ok = all(
        audit.verify_inclusion(event, i, audit.inclusion_proof(i), root)
        for i, event in enumerate(audit.events())
    )  # vacuously True for an empty log

    root_matches = None if expect_root is None else (root == expect_root)
    ok = ledger_balanced and proofs_ok and (root_matches is not False)

    return VerificationResult(
        ledger_balanced=ledger_balanced,
        currencies=currencies,
        posting_count=len(ledger.postings()),
        audit_events=audit.size,
        audit_root=root,
        proofs_ok=proofs_ok,
        root_matches_expected=root_matches,
        ok=ok,
    )
