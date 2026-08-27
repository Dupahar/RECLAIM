"""Replay verification — re-check the integrity of a stored run.

Loads a persisted ledger + audit log and re-verifies, from scratch, that:
- the ledger is globally balanced (double-entry invariant intact),
- every audit event has a valid inclusion proof under the recomputed root, and
- (optionally) the audit root matches a previously-published root.

- (optionally) the log is *consistent* with every root published earlier.

The last two checks are the real tamper-detection story. Comparing against a
published root catches a log that was altered since it was published. Comparing
against the *history* of published roots catches more: a consistency proof shows
the current log is an append-only extension of each earlier one, so an entry
rewritten or removed between publications cannot hide behind a freshly
recomputed, internally valid root.

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
    heads_checked: int = 0                 # prior published heads verified against
    consistency_ok: Optional[bool] = None  # None if no prior heads were supplied


def verify_stores(ledger_store: EventStore, audit_store: EventStore,
                  expect_root: Optional[str] = None,
                  prior_heads: Optional[list] = None) -> VerificationResult:
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

    # Consistency against every previously published head. An inclusion proof
    # cannot show that history was not rewritten between publications -- only a
    # consistency proof can, so a log that has ever been published is checked
    # against each of those older roots as well.
    consistency_ok: Optional[bool] = None
    heads_checked = 0
    if prior_heads:
        consistency_ok = True
        for size, published_root in prior_heads:
            if size > audit.size:
                consistency_ok = False       # the log shrank -- entries were removed
                heads_checked += 1
                continue
            proof = audit.consistency_proof(size)
            if not audit.verify_consistency(size, published_root, proof, root):
                consistency_ok = False
            heads_checked += 1

    ok = (ledger_balanced and proofs_ok and (root_matches is not False)
          and (consistency_ok is not False))

    return VerificationResult(
        ledger_balanced=ledger_balanced,
        currencies=currencies,
        posting_count=len(ledger.postings()),
        audit_events=audit.size,
        audit_root=root,
        proofs_ok=proofs_ok,
        root_matches_expected=root_matches,
        ok=ok,
        heads_checked=heads_checked,
        consistency_ok=consistency_ok,
    )
