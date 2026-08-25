"""Phase 11 tests — durable persistence (event-sourced ledger + audit)."""
from datetime import datetime

import pytest

from reclaim.money import Money
from reclaim.domain import Direction, LedgerEntry
from reclaim.ledger import Ledger, Posting, DuplicatePostingError
from reclaim.audit import AuditEvent, MerkleAuditLog
from reclaim.persistence import (
    AuditRepository,
    InMemoryStore,
    JsonlFileStore,
    LedgerRepository,
    PersistenceError,
    posting_from_record,
    posting_to_record,
    event_from_record,
    event_to_record,
    _money_from,
    _money_to,
)

TS = datetime(2026, 8, 26, 9, 0, 0)


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def posting(pid="p1"):
    return Posting(id=pid, ts=TS, entries=(
        LedgerEntry(f"{pid}:e1", pid, "bank_account", Direction.DEBIT, inr("488200.00"), TS),
        LedgerEntry(f"{pid}:e2", pid, "fee_expense", Direction.DEBIT, inr("11800.00"), TS),
        LedgerEntry(f"{pid}:e3", pid, "sales_clearing", Direction.CREDIT, inr("500000.00"), TS),
    ), memo="settlement")


def event(kind="match", **detail):
    return AuditEvent(kind=kind, at=TS, detail=detail)


# --------------------------------------------------------------------------
# Serialization round-trips
# --------------------------------------------------------------------------
def test_posting_record_roundtrip():
    p = posting()
    assert posting_from_record(posting_to_record(p)) == p


def test_event_record_roundtrip():
    e = event("recovery", leak="L1", amount="499.00")
    assert event_from_record(event_to_record(e)) == e


def test_money_precision_preserved_through_record():
    # Full decimal precision survives serialization at the Money level.
    # (Ledger entries must be rounded; unrounded Money is still exact here.)
    m = Money.of("95.6640", "INR")
    back = _money_from(_money_to(m), "x")
    assert back == m
    assert str(back.amount) == "95.6640"    # trailing precision preserved exactly


# --------------------------------------------------------------------------
# Ledger repository — rehydrate identical balances
# --------------------------------------------------------------------------
def test_ledger_repository_inmemory_roundtrip():
    store = InMemoryStore()
    repo = LedgerRepository(store)
    repo.save_posting(posting("p1"))
    repo.save_posting(Posting(id="p2", ts=TS, entries=(
        LedgerEntry("p2:a", "p2", "cash", Direction.DEBIT, inr("250.00"), TS),
        LedgerEntry("p2:b", "p2", "revenue", Direction.CREDIT, inr("250.00"), TS),
    )))
    ledger = repo.load()
    assert ledger.is_globally_balanced("INR")
    assert ledger.balance("bank_account", "INR") == inr("488200.00")
    assert ledger.balance("cash", "INR") == inr("250.00")
    assert len(ledger.postings()) == 2


def test_ledger_repository_file_durable(tmp_path):
    path = tmp_path / "ledger.jsonl"
    LedgerRepository(JsonlFileStore(path)).save_posting(posting("p1"))
    # a brand-new repository/process reading the same file must see the state
    ledger = LedgerRepository(JsonlFileStore(path)).load()
    assert ledger.balance("bank_account", "INR") == inr("488200.00")
    assert path.exists()


def test_file_store_is_append_only(tmp_path):
    path = tmp_path / "ledger.jsonl"
    repo = LedgerRepository(JsonlFileStore(path))
    repo.save_posting(posting("p1"))
    first = path.read_text(encoding="utf-8")
    repo.save_posting(Posting(id="p2", ts=TS, entries=(
        LedgerEntry("p2:a", "p2", "cash", Direction.DEBIT, inr("1.00"), TS),
        LedgerEntry("p2:b", "p2", "revenue", Direction.CREDIT, inr("1.00"), TS),
    )))
    second = path.read_text(encoding="utf-8")
    assert second.startswith(first)          # earlier bytes never rewritten
    assert second.count("\n") == 2


def test_reload_is_deterministic(tmp_path):
    path = tmp_path / "l.jsonl"
    repo = LedgerRepository(JsonlFileStore(path))
    repo.save_posting(posting("p1"))
    a = repo.load()
    b = repo.load()
    assert a.balance("sales_clearing", "INR") == b.balance("sales_clearing", "INR")


# --------------------------------------------------------------------------
# Audit repository — rehydrate identical Merkle root
# --------------------------------------------------------------------------
def test_audit_repository_roundtrip_preserves_root(tmp_path):
    original = MerkleAuditLog()
    repo = AuditRepository(JsonlFileStore(tmp_path / "audit.jsonl"))
    for i in range(5):
        e = event("match", pair=f"s{i}~b{i}", amount=f"{i}.00")
        original.append(e)
        repo.append_event(e)
    reloaded = repo.load()
    assert reloaded.root() == original.root()          # identical tamper-evidence anchor
    # inclusion proofs from the reloaded log still verify
    root = reloaded.root()
    for i, e in enumerate(reloaded.events()):
        assert reloaded.verify_inclusion(e, i, reloaded.inclusion_proof(i), root)


# --------------------------------------------------------------------------
# Corruption / integrity
# --------------------------------------------------------------------------
def test_corrupt_json_line_raises(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id":"p1"}\nnot-json\n', encoding="utf-8")
    with pytest.raises(PersistenceError):
        LedgerRepository(JsonlFileStore(path)).load()


def test_malformed_posting_record_raises():
    store = InMemoryStore()
    store.append({"id": "p1", "ts": TS.isoformat(), "entries": [{"bad": "record"}]})
    with pytest.raises(PersistenceError):
        LedgerRepository(store).load()


def test_posting_record_missing_top_field_raises():
    # entries valid, but the posting is missing its 'id' -> KeyError -> PersistenceError
    store = InMemoryStore()
    store.append({"ts": TS.isoformat(), "memo": "", "entries": [
        {"id": "a", "txn_id": "p", "account": "x", "direction": "debit",
         "amount": {"amount": "1.00", "currency": "INR"}, "ts": TS.isoformat()},
        {"id": "b", "txn_id": "p", "account": "y", "direction": "credit",
         "amount": {"amount": "1.00", "currency": "INR"}, "ts": TS.isoformat()},
    ]})
    with pytest.raises(PersistenceError):
        LedgerRepository(store).load()


def test_malformed_event_record_raises():
    store = InMemoryStore()
    store.append({"kind": "x"})  # missing 'at'
    with pytest.raises(PersistenceError):
        AuditRepository(store).load()


def test_invalid_money_record_raises():
    store = InMemoryStore()
    store.append({"id": "p", "ts": TS.isoformat(), "memo": "", "entries": [
        {"id": "a", "txn_id": "p", "account": "x", "direction": "debit",
         "amount": {"amount": "not-a-number", "currency": "INR"}, "ts": TS.isoformat()},
        {"id": "b", "txn_id": "p", "account": "y", "direction": "credit",
         "amount": {"amount": "1.00", "currency": "INR"}, "ts": TS.isoformat()},
    ]})
    with pytest.raises(PersistenceError):
        LedgerRepository(store).load()


def test_duplicate_conflicting_posting_detected_on_load():
    # two records with the same id but different content -> ledger rejects on replay
    store = InMemoryStore()
    store.append(posting_to_record(posting("p1")))
    conflicting = Posting(id="p1", ts=TS, entries=(
        LedgerEntry("p1:x", "p1", "a", Direction.DEBIT, inr("1.00"), TS),
        LedgerEntry("p1:y", "p1", "b", Direction.CREDIT, inr("1.00"), TS),
    ))
    store.append(posting_to_record(conflicting))
    with pytest.raises(DuplicatePostingError):
        LedgerRepository(store).load()


def test_empty_file_loads_empty(tmp_path):
    ledger = LedgerRepository(JsonlFileStore(tmp_path / "nope.jsonl")).load()
    assert ledger.postings() == ()


def test_blank_lines_are_skipped(tmp_path):
    # a file with blank lines between records must still load cleanly
    path = tmp_path / "gappy.jsonl"
    rec = posting_to_record(posting("p1"))
    import json as _json
    path.write_text("\n" + _json.dumps(rec) + "\n\n", encoding="utf-8")
    ledger = LedgerRepository(JsonlFileStore(path)).load()
    assert len(ledger.postings()) == 1


def test_inmemory_store_isolates_history():
    store = InMemoryStore()
    rec = {"kind": "x", "at": TS.isoformat(), "detail": {}}
    store.append(rec)
    rec["kind"] = "MUTATED"          # mutate the caller's dict after append
    assert store.read()[0]["kind"] == "x"   # stored copy is unaffected
