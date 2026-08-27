"""The Leak Ledger — the seam between reconciliation and recovery.

Architecture Layer 2 (§5.2) calls this "the integration contract between
reconciliation (writer) and recovery (reader/writer), and the object the human
exception queue renders. Nothing leaves the system undocumented (G2)."

Until now a run's leaks lived only inside an in-memory ``RunReport``: they were
reported, then discarded when the process exited. That made three things
impossible — a durable human exception queue, recovery state that survives a
restart, and any question of the form "what is still open?".

**Append-only, like the money ledger.** A leak's state changes over its life
(detected -> pending -> recovered / exhausted / halted), and this store never
edits a record to reflect that. Each change is appended as a **new version** of
the leak, exactly as a ledger correction is a new posting rather than an edit
(G3). ``current()`` reads the latest version; ``history()`` reads them all, so
"why is this leak in this state?" is always answerable.

Idempotent by construction: re-recording a leak identical to its current version
is a no-op, so replaying a run cannot inflate the queue.
"""
from __future__ import annotations

from dataclasses import replace

from .domain import LeakRecord, RecoveryState
from .money import Money


class LeakLedgerError(Exception):
    """Raised on an invalid Leak Ledger operation."""


class LeakLedger:
    """An append-only, versioned store of every rupee that failed to reconcile."""

    def __init__(self) -> None:
        # leak id -> ordered versions, oldest first. dict preserves insertion
        # order, which keeps every read deterministic (G4).
        self._versions: dict[str, list[LeakRecord]] = {}

    # ---- writes -------------------------------------------------------
    def record(self, leak: LeakRecord) -> LeakRecord:
        """Append a leak, or a new version of one. Idempotent by content."""
        if not isinstance(leak, LeakRecord):
            raise LeakLedgerError("record() requires a LeakRecord")
        versions = self._versions.setdefault(leak.id, [])
        if versions and versions[-1] == leak:
            return versions[-1]          # identical replay -- no new version
        versions.append(leak)
        return leak

    def record_many(self, leaks) -> None:
        for leak in leaks:
            self.record(leak)

    def transition(self, leak_id: str, state: RecoveryState) -> LeakRecord:
        """Append a new version of a leak with an updated recovery state.

        The prior version stays readable via ``history`` — this is how recovery
        writes back without ever mutating what reconciliation wrote.
        """
        if not isinstance(state, RecoveryState):
            raise LeakLedgerError("transition() requires a RecoveryState")
        current = self.current(leak_id)
        if current is None:
            raise LeakLedgerError(f"unknown leak id {leak_id!r}")
        return self.record(replace(current, recovery_state=state))

    # ---- reads --------------------------------------------------------
    def current(self, leak_id: str):
        """The latest version of a leak, or None if it was never recorded."""
        versions = self._versions.get(leak_id)
        return versions[-1] if versions else None

    def history(self, leak_id: str) -> tuple[LeakRecord, ...]:
        """Every recorded version, oldest first — the leak's own audit trail."""
        return tuple(self._versions.get(leak_id, ()))

    def leaks(self) -> tuple[LeakRecord, ...]:
        """The current version of every leak, in first-seen order."""
        return tuple(versions[-1] for versions in self._versions.values())

    @property
    def size(self) -> int:
        return len(self._versions)

    @property
    def version_count(self) -> int:
        """Total versions across all leaks — what the durable store holds."""
        return sum(len(v) for v in self._versions.values())

    # ---- the classifier split (architecture Layer 4) -------------------
    def recoverable(self) -> tuple[LeakRecord, ...]:
        """Leaks recovery may act on."""
        return tuple(l for l in self.leaks() if l.recoverable)

    def accounting_exceptions(self) -> tuple[LeakRecord, ...]:
        """Leaks nobody will chase — they need explaining, not recovering."""
        return tuple(l for l in self.leaks() if not l.recoverable)

    def by_state(self, state: RecoveryState) -> tuple[LeakRecord, ...]:
        return tuple(l for l in self.leaks() if l.recovery_state is state)

    def open_queue(self) -> tuple[LeakRecord, ...]:
        """What a human still owns: nothing recovered, nothing written off.

        This is the object the HITL exception console renders.
        """
        settled = (
            RecoveryState.RECOVERED,        # the money came back
            RecoveryState.NOT_RECOVERABLE,  # dead mandate -- booked as churn, not chased
            RecoveryState.SUPERSEDED,       # a later match resolved it; never a real leak
        )
        return tuple(l for l in self.leaks() if l.recovery_state not in settled)

    def total(self, currency: str) -> Money:
        """Summed amount of the current versions, for one currency."""
        total = Money.zero(currency)
        for leak in self.leaks():
            if leak.amount.currency == currency:
                total = total + leak.amount
        return total

    def currencies(self) -> tuple[str, ...]:
        return tuple(sorted({l.amount.currency for l in self.leaks()}))
