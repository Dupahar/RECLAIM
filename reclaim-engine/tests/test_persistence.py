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


# --------------------------------------------------------------------------
# Run-level idempotency — persisting the same run twice must be a no-op, or a
# legitimate re-persist would change the root and look exactly like tampering.
# --------------------------------------------------------------------------
def test_audit_repository_append_is_idempotent():
    store = InMemoryStore()
    repo = AuditRepository(store)
    event = AuditEvent("exact_match", TS, {"settlement": "s1", "bank": "b1"})
    repo.append_event(event)
    repo.append_event(event)                      # identical -> dropped
    assert len(store.read()) == 1
    assert repo.load().size == 1


def test_audit_repository_dedupes_against_pre_existing_store():
    """A fresh repository over a store that already holds the event also skips."""
    store = InMemoryStore()
    event = AuditEvent("residual_leak", TS, {"leak": "L1"})
    AuditRepository(store).append_event(event)
    AuditRepository(store).append_event(event)    # new instance, same content
    assert len(store.read()) == 1


def test_audit_repository_keeps_distinct_events():
    store = InMemoryStore()
    repo = AuditRepository(store)
    repo.append_event(AuditEvent("residual_leak", TS, {"leak": "L1"}))
    repo.append_event(AuditEvent("residual_leak", TS, {"leak": "L2"}))
    assert len(store.read()) == 2
    assert repo.load().size == 2


def test_ledger_repository_save_is_idempotent():
    store = InMemoryStore()
    repo = LedgerRepository(store)
    repo.save_posting(posting())
    repo.save_posting(posting())
    assert len(store.read()) == 1
    assert len(repo.load().postings()) == 1


# --------------------------------------------------------------------------
# Leak Ledger durability (Phase 19)
# --------------------------------------------------------------------------
from reclaim.domain import LeakRecord, LeakType, RecoveryState
from reclaim.leak_ledger import LeakLedger
from reclaim.persistence import LeakRepository, leak_from_record, leak_to_record


def _leak(lid="L1", state=RecoveryState.NONE, **kw):
    return LeakRecord(id=lid, amount=inr("250.00"), leak_type=LeakType.SHORT_PAYMENT,
                      source_refs=("s1", "b1"), hypothesis="short by 250",
                      recoverable=True, recovery_state=state, **kw)


def test_leak_round_trips_through_a_record():
    original = _leak(evidence=("bank credit 2800 < expected 3000",), audit_ref="deadbeef:2")
    assert leak_from_record(leak_to_record(original)) == original


def test_leak_record_rejects_corrupt_data():
    with pytest.raises(PersistenceError):
        leak_from_record({"id": "L1"}, "leak[0]")


def test_leak_repository_rehydrates_state_and_history():
    store = InMemoryStore()
    source = LeakLedger()
    source.record(_leak())
    source.transition("L1", RecoveryState.RECOVERED)
    LeakRepository(store).save_ledger(source)

    reloaded = LeakRepository(store).load()
    assert reloaded.current("L1").recovery_state is RecoveryState.RECOVERED
    assert [v.recovery_state.value for v in reloaded.history("L1")] == ["none", "recovered"]
    assert reloaded.version_count == source.version_count


def test_leak_repository_save_is_idempotent():
    store = InMemoryStore()
    ledger = LeakLedger()
    ledger.record(_leak())
    LeakRepository(store).save_ledger(ledger)
    LeakRepository(store).save_ledger(ledger)
    assert len(store.read()) == 1


def test_leak_repository_load_of_empty_store():
    assert LeakRepository(InMemoryStore()).load().size == 0


def test_leak_record_propagates_a_nested_money_error():
    """A bad amount surfaces as the inner PersistenceError, not a re-wrap."""
    bad = leak_to_record(_leak())
    bad["amount"] = {"amount": "not-a-number", "currency": "INR"}
    with pytest.raises(PersistenceError, match="invalid money"):
        leak_from_record(bad, "leak[0]")


# --------------------------------------------------------------------------
# Phase 25 — the HITL checkpointer's storage
# --------------------------------------------------------------------------
def _gate(gid="g1", **kw):
    from reclaim.control import Gate, GateKind
    base = dict(id=gid, kind=GateKind.RECOVERY_HALT, subject_ref="leak:short:s1",
                question="Authorise one further attempt?",
                opened_at=datetime(2026, 8, 31, 9, 0, 0),
                amount=Money.of("200.00", "INR"), evidence=("halted after 1 attempt",))
    base.update(kw)
    return Gate(**base)


def test_gate_record_round_trips_including_the_decision():
    from reclaim.control import ControlPlane, GateState
    from reclaim.persistence import GateRepository

    plane = ControlPlane()
    plane.open_gate(_gate())
    plane.approve("g1", actor="ops@example.com",
                  at=datetime(2026, 8, 31, 17, 30, 0), rationale="customer confirmed")
    store = InMemoryStore()
    GateRepository(store).save_plane(plane)

    reloaded = GateRepository(store).load()
    assert reloaded.history("g1") == plane.history("g1")
    assert reloaded.current("g1").state is GateState.APPROVED
    assert reloaded.current("g1").decided_by == "ops@example.com"


def test_a_gate_with_no_amount_round_trips():
    from reclaim.control import ControlPlane
    from reclaim.persistence import GateRepository

    plane = ControlPlane()
    plane.open_gate(_gate(amount=None))
    store = InMemoryStore()
    GateRepository(store).save_plane(plane)
    assert GateRepository(store).load().current("g1").amount is None


def test_re_persisting_a_plane_is_a_no_op():
    from reclaim.control import ControlPlane
    from reclaim.persistence import GateRepository

    plane = ControlPlane()
    plane.open_gate(_gate())
    store = InMemoryStore()
    GateRepository(store).save_plane(plane)
    GateRepository(store).save_plane(plane)
    assert len(store.read()) == 1


def test_a_log_whose_gate_opens_already_approved_is_refused():
    """A decision with no open version behind it means the log lost the
    checkpoint. Rehydrating it would invent an authorisation nobody gave."""
    from reclaim.control import GateState
    from reclaim.persistence import GateRepository, gate_to_record

    orphan = _gate(state=GateState.APPROVED, decided_by="ops@x",
                   decided_at=datetime(2026, 8, 31, 17, 30, 0))
    store = InMemoryStore()
    store.append(gate_to_record(orphan))
    with pytest.raises(PersistenceError) as exc:
        GateRepository(store).load()
    assert "open version is missing" in str(exc.value)


def test_a_corrupt_gate_record_is_rejected():
    from reclaim.persistence import GateRepository

    store = InMemoryStore()
    store.append({"id": "g1", "kind": "not_a_kind", "subject_ref": "x",
                  "question": "q", "opened_at": "2026-08-31T09:00:00"})
    with pytest.raises(PersistenceError):
        GateRepository(store).load()


def test_a_gate_record_missing_its_question_is_rejected():
    from reclaim.control import ControlError
    from reclaim.persistence import GateRepository

    store = InMemoryStore()
    store.append({"id": "g1", "kind": "recovery_halt", "subject_ref": "x",
                  "question": "", "opened_at": "2026-08-31T09:00:00"})
    with pytest.raises(ControlError):
        GateRepository(store).load()


def test_a_gate_record_with_bad_money_is_rejected():
    from reclaim.persistence import GateRepository

    store = InMemoryStore()
    store.append({"id": "g1", "kind": "recovery_halt", "subject_ref": "x",
                  "question": "q", "opened_at": "2026-08-31T09:00:00",
                  "amount": {"amount": "not-a-number", "currency": "INR"}})
    with pytest.raises(PersistenceError):
        GateRepository(store).load()


def test_the_checkpointer_survives_a_restart_on_disk(tmp_path):
    """The point of the whole module: a paused decision outlives the process."""
    from reclaim.control import ControlPlane, GateState
    from reclaim.persistence import GateRepository, JsonlFileStore

    path = tmp_path / "gates.jsonl"
    plane = ControlPlane()
    plane.open_gate(_gate())
    GateRepository(JsonlFileStore(path)).save_plane(plane)

    # ... a day later, a different process ...
    resumed = GateRepository(JsonlFileStore(path)).load()
    assert [g.id for g in resumed.awaiting()] == ["g1"]
    resumed.approve("g1", actor="ops@example.com",
                    at=datetime(2026, 9, 1, 10, 0, 0), rationale="checked with the bank")
    GateRepository(JsonlFileStore(path)).save_plane(resumed)

    final = GateRepository(JsonlFileStore(path)).load()
    assert final.current("g1").state is GateState.APPROVED
    assert final.awaiting() == ()
    assert len(final.history("g1")) == 2
