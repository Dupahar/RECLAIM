"""Durable persistence — event-sourced storage for the ledger and audit log.

Both the double-entry ledger and the Merkle audit log are append-only and
deterministically replayable, which makes **event sourcing** the natural fit:
persist an append-only stream of records, and rehydrate the *exact* same state
(identical balances, identical Merkle root). Corrections are new records, never
edits (goal **G3**).

Two stores implement one interface:
- ``InMemoryStore`` — for tests and ephemeral use.
- ``JsonlFileStore`` — durable, append-only JSON Lines on disk (one record per
  line; the file is only ever appended to).

Dependency-light: standard-library ``json``/``pathlib`` only.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime
from typing import Protocol, runtime_checkable

from .audit import AuditEvent, MerkleAuditLog
from .domain import Direction, LeakRecord, LeakType, LedgerEntry, RecoveryState
from .leak_ledger import LeakLedger
from .ledger import Ledger, Posting, LedgerError
from .money import Money, MoneyError


class PersistenceError(Exception):
    """Raised when stored data is malformed or cannot be rehydrated."""


@runtime_checkable
class EventStore(Protocol):
    def append(self, record: dict) -> None:  # pragma: no cover - protocol
        ...

    def read(self) -> list[dict]:  # pragma: no cover - protocol
        ...


class InMemoryStore:
    """A simple append-only in-memory store."""

    def __init__(self) -> None:
        self._records: list[dict] = []

    def append(self, record: dict) -> None:
        # store a JSON round-tripped copy so callers can't mutate history
        self._records.append(json.loads(json.dumps(record)))

    def read(self) -> list[dict]:
        return [json.loads(json.dumps(r)) for r in self._records]


class JsonlFileStore:
    """Durable append-only JSON Lines store (one record per line)."""

    def __init__(self, path) -> None:
        self._path = pathlib.Path(path)

    def append(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":"), sort_keys=True)
        with self._path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")

    def read(self) -> list[dict]:
        if not self._path.exists():
            return []
        out: list[dict] = []
        for lineno, raw in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            if raw.strip() == "":
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise PersistenceError(f"corrupt record at line {lineno}: {exc}") from exc
        return out


# --------------------------------------------------------------------------
# (de)serialization
# --------------------------------------------------------------------------
def _money_to(m: Money) -> dict:
    return {"amount": str(m.amount), "currency": m.currency}


def _money_from(d, where: str) -> Money:
    try:
        return Money.of(d["amount"], d["currency"])
    except (KeyError, TypeError, MoneyError) as exc:
        raise PersistenceError(f"{where}: invalid money {d!r} ({exc})") from exc


def _entry_to(e: LedgerEntry) -> dict:
    return {"id": e.id, "txn_id": e.txn_id, "account": e.account,
            "direction": e.direction.value, "amount": _money_to(e.amount),
            "ts": e.ts.isoformat()}


def _entry_from(d, where: str) -> LedgerEntry:
    try:
        return LedgerEntry(id=d["id"], txn_id=d["txn_id"], account=d["account"],
                           direction=Direction(d["direction"]),
                           amount=_money_from(d["amount"], f"{where}.amount"),
                           ts=datetime.fromisoformat(d["ts"]))
    except PersistenceError:
        raise
    except Exception as exc:
        raise PersistenceError(f"{where}: invalid ledger entry ({exc})") from exc


def posting_to_record(p: Posting) -> dict:
    return {"id": p.id, "ts": p.ts.isoformat(), "memo": p.memo,
            "entries": [_entry_to(e) for e in p.entries]}


def posting_from_record(d, where: str = "posting") -> Posting:
    try:
        entries = tuple(_entry_from(e, f"{where}.entries[{i}]")
                        for i, e in enumerate(d["entries"]))
        return Posting(id=d["id"], ts=datetime.fromisoformat(d["ts"]),
                       entries=entries, memo=d.get("memo", ""))
    except (PersistenceError, LedgerError):
        raise
    except Exception as exc:
        raise PersistenceError(f"{where}: invalid posting ({exc})") from exc


def leak_to_record(l: LeakRecord) -> dict:
    return {"id": l.id, "amount": _money_to(l.amount), "leak_type": l.leak_type.value,
            "source_refs": list(l.source_refs), "hypothesis": l.hypothesis,
            "confidence": l.confidence, "recoverable": l.recoverable,
            "recovery_state": l.recovery_state.value,
            "evidence": list(l.evidence), "audit_ref": l.audit_ref}


def leak_from_record(d, where: str = "leak") -> LeakRecord:
    try:
        return LeakRecord(
            id=d["id"],
            amount=_money_from(d["amount"], f"{where}.amount"),
            leak_type=LeakType(d["leak_type"]),
            source_refs=tuple(d.get("source_refs", ())),
            hypothesis=d.get("hypothesis", ""),
            confidence=d.get("confidence"),
            recoverable=d.get("recoverable", False),
            recovery_state=RecoveryState(d.get("recovery_state", "none")),
            evidence=tuple(d.get("evidence", ())),
            audit_ref=d.get("audit_ref"),
        )
    except PersistenceError:
        raise
    except Exception as exc:
        raise PersistenceError(f"{where}: invalid leak record ({exc})") from exc


def event_to_record(e: AuditEvent) -> dict:
    return {"kind": e.kind, "at": e.at.isoformat(), "detail": dict(e.detail)}


def event_from_record(d, where: str = "event") -> AuditEvent:
    try:
        return AuditEvent(kind=d["kind"], at=datetime.fromisoformat(d["at"]),
                          detail=dict(d.get("detail", {})))
    except Exception as exc:
        raise PersistenceError(f"{where}: invalid audit event ({exc})") from exc


# --------------------------------------------------------------------------
# Repositories
# --------------------------------------------------------------------------
def _canonical(record: dict) -> str:
    """Canonical text form of a record — the dedupe key. Matches the on-disk
    form written by ``JsonlFileStore.append``."""
    return json.dumps(record, separators=(",", ":"), sort_keys=True)


class _DedupingRepository:
    """Shared base: append a record only if that exact record is not already
    stored. This makes persisting the *same* run twice a no-op, so a re-run
    cannot silently change the rehydrated state or the Merkle root — the same
    idempotency rule ``Ledger.post`` already applies in memory.

    The set of stored records is snapshotted on first write and kept current as
    this repository appends, so a persist costs one store read, not one per
    record.
    """

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._seen = None

    def _append_once(self, record: dict) -> bool:
        """Append unless already present. Returns True if it was written."""
        if self._seen is None:
            self._seen = {_canonical(r) for r in self._store.read()}
        key = _canonical(record)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._store.append(record)
        return True


class LedgerRepository(_DedupingRepository):
    """Persists postings and rehydrates a Ledger with identical balances.
    Idempotent by content: re-saving an identical posting is a no-op."""

    def save_posting(self, posting: Posting) -> None:
        self._append_once(posting_to_record(posting))

    def load(self) -> Ledger:
        ledger = Ledger()
        for i, rec in enumerate(self._store.read()):
            ledger.post(posting_from_record(rec, f"posting[{i}]"))
        return ledger


class LeakRepository(_DedupingRepository):
    """Persists Leak Ledger versions and rehydrates an identical LeakLedger.

    Every *version* is stored, in order, so the rehydrated ledger reproduces the
    same current state **and** the same history. Idempotent by content, so
    re-persisting a run neither duplicates leaks nor invents a state change.
    """

    def save_leak(self, leak: LeakRecord) -> None:
        self._append_once(leak_to_record(leak))

    def save_ledger(self, ledger: LeakLedger) -> None:
        for leak in ledger.leaks():
            for version in ledger.history(leak.id):
                self.save_leak(version)

    def load(self) -> LeakLedger:
        ledger = LeakLedger()
        for i, rec in enumerate(self._store.read()):
            ledger.record(leak_from_record(rec, f"leak[{i}]"))
        return ledger


class AuditRepository(_DedupingRepository):
    """Persists audit events and rehydrates a MerkleAuditLog with identical root.
    Idempotent by content: re-appending an identical event is a no-op, so
    persisting a run twice leaves the root unchanged."""

    def append_event(self, event: AuditEvent) -> None:
        self._append_once(event_to_record(event))

    def load(self) -> MerkleAuditLog:
        log = MerkleAuditLog()
        for i, rec in enumerate(self._store.read()):
            log.append(event_from_record(rec, f"event[{i}]"))
        return log
