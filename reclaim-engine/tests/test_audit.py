"""Phase 10 tests — Merkle transparency audit log."""
import hashlib
from datetime import datetime

import pytest

from reclaim.audit import (
    AuditEvent,
    MerkleAuditLog,
    _leaf_hash,
    _node_hash,
)

TS = datetime(2026, 8, 25, 9, 0, 0)


def ev(kind, **detail):
    return AuditEvent(kind=kind, at=TS, detail=detail)


def _log(n):
    log = MerkleAuditLog()
    for i in range(n):
        log.append(ev("match", pair=f"s{i}~b{i}", amount=f"{i}.00"))
    return log


# --------------------------------------------------------------------------
# Hashing primitives & known roots
# --------------------------------------------------------------------------
def test_empty_log_root_is_hash_of_empty_string():
    assert MerkleAuditLog().root() == hashlib.sha256(b"").hexdigest()


def test_single_event_root_is_leaf_hash():
    log = MerkleAuditLog()
    e = ev("match", pair="s0~b0")
    log.append(e)
    assert log.root() == _leaf_hash(e.canonical_bytes()).hex()


def test_two_event_root_is_node_of_leaves():
    log = MerkleAuditLog()
    e0, e1 = ev("a"), ev("b")
    log.append(e0)
    log.append(e1)
    expected = _node_hash(_leaf_hash(e0.canonical_bytes()), _leaf_hash(e1.canonical_bytes()))
    assert log.root() == expected.hex()


# --------------------------------------------------------------------------
# Canonical serialization is order-independent
# --------------------------------------------------------------------------
def test_canonical_bytes_independent_of_detail_order():
    a = AuditEvent("match", TS, {"x": "1", "y": "2"})
    b = AuditEvent("match", TS, {"y": "2", "x": "1"})
    assert a.canonical_bytes() == b.canonical_bytes()


# --------------------------------------------------------------------------
# Inclusion proofs verify for every index across many sizes
# --------------------------------------------------------------------------
@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8, 9])
def test_inclusion_proofs_verify_for_all_indices(n):
    log = _log(n)
    root = log.root()
    for i in range(n):
        proof = log.inclusion_proof(i)
        assert log.verify_inclusion(log.events()[i], i, proof, root) is True


def test_proof_length_is_logarithmic():
    log = _log(8)
    assert len(log.inclusion_proof(0)) == 3   # log2(8)


# --------------------------------------------------------------------------
# Tamper evidence
# --------------------------------------------------------------------------
def test_tampered_event_fails_verification():
    log = _log(5)
    root = log.root()
    proof = log.inclusion_proof(2)
    tampered = ev("match", pair="s2~b2", amount="999999.00")   # different from what was logged
    assert log.verify_inclusion(tampered, 2, proof, root) is False


def test_editing_a_past_entry_changes_the_root():
    a = MerkleAuditLog()
    for e in (ev("x", n="0"), ev("x", n="1"), ev("x", n="2")):
        a.append(e)
    root_before = a.root()

    b = MerkleAuditLog()
    for e in (ev("x", n="0"), ev("x", n="TAMPERED"), ev("x", n="2")):
        b.append(e)
    assert b.root() != root_before          # any past edit is detectable


def test_verify_rejects_wrong_index_and_root():
    log = _log(4)
    proof = log.inclusion_proof(1)
    # right event & proof, wrong root
    assert log.verify_inclusion(log.events()[1], 1, proof, "00" * 32) is False
    # right event, wrong index (out of range)
    assert log.verify_inclusion(log.events()[1], 99, proof, log.root()) is False


# --------------------------------------------------------------------------
# Append-only / prefix (consistency) checking
# --------------------------------------------------------------------------
def test_root_at_prefix_matches_independent_log():
    log = _log(7)
    # root over the first 4 entries must equal a fresh 4-entry log's root
    prefix_root = log.root_at(4)
    assert prefix_root == _log(4).root()


def test_root_at_full_size_equals_root():
    log = _log(6)
    assert log.root_at(6) == log.root()


def test_root_at_out_of_range():
    log = _log(3)
    with pytest.raises(IndexError):
        log.root_at(4)
    with pytest.raises(IndexError):
        log.root_at(-1)


# --------------------------------------------------------------------------
# Misc guards & determinism
# --------------------------------------------------------------------------
def test_append_type_guard():
    with pytest.raises(TypeError):
        MerkleAuditLog().append("not-an-event")  # type: ignore[arg-type]


def test_inclusion_proof_out_of_range():
    log = _log(3)
    with pytest.raises(IndexError):
        log.inclusion_proof(3)


def test_determinism_same_events_same_root():
    assert _log(5).root() == _log(5).root()


def test_size_and_events():
    log = _log(3)
    assert log.size == 3
    assert len(log.events()) == 3
