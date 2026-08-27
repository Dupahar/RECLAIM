"""Phase 10 tests — Merkle transparency audit log."""
import hashlib
from datetime import datetime

import pytest

from reclaim.audit import (
    AuditEvent,
    MerkleAuditLog,
    _leaf_hash,
    _node_hash,
    _verify_consistency,
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


# --------------------------------------------------------------------------
# Consistency proofs (Phase 20) — RFC 6962's other half.
#
# An inclusion proof answers "is this event in a tree with root R?". It cannot
# answer "is R's history the same history as the one I saw last week?" — delete
# an entry and the survivors form a smaller tree that proves inclusion perfectly
# well. A consistency proof is what closes that gap.
# --------------------------------------------------------------------------
def _log_of(values):
    log = MerkleAuditLog()
    for v in values:
        log.append(ev("decision", v=v))
    return log


def test_consistency_proof_over_every_prefix_pair():
    """Exhaustive: every (old_size, new_size) pair up to 33 entries verifies."""
    roots = [_log(m).root() for m in range(34)]      # same generator as below

    for n in range(0, 34):
        log = _log(n)
        for m in range(0, n + 1):
            proof = log.consistency_proof(m)
            assert log.verify_consistency(m, roots[m], proof), f"({m}, {n}) failed"


def test_consistency_detects_a_rewritten_entry():
    old = _log_of(["a", "b", "c", "d"])
    old_root = old.root()
    tampered = _log_of(["a", "ROGUE", "c", "d", "e", "f", "g", "h"])
    assert tampered.verify_consistency(4, old_root, tampered.consistency_proof(4)) is False


def test_inclusion_alone_cannot_catch_that_rewrite():
    """Why consistency proofs exist, stated as a test."""
    tampered = _log_of(["a", "ROGUE", "c", "d", "e", "f", "g", "h"])
    event = tampered.events()[1]
    # the rewritten log is entirely self-consistent...
    assert tampered.verify_inclusion(event, 1, tampered.inclusion_proof(1), tampered.root())
    # ...and only the published earlier root exposes it
    old_root = _log_of(["a", "b", "c", "d"]).root()
    assert not tampered.verify_consistency(4, old_root, tampered.consistency_proof(4))


def test_consistency_detects_deletion_and_reordering():
    old_root = _log_of(["a", "b", "c", "d"]).root()
    dropped = _log_of(["a", "c", "d", "e", "f", "g", "h"])
    assert not dropped.verify_consistency(4, old_root, dropped.consistency_proof(4))
    swapped = _log_of(["b", "a", "c", "d", "e", "f", "g", "h"])
    assert not swapped.verify_consistency(4, old_root, swapped.consistency_proof(4))


def test_consistency_with_the_empty_tree_and_equal_sizes():
    log = _log(5)
    assert log.consistency_proof(0) == []
    assert log.verify_consistency(0, MerkleAuditLog().root(), [])
    assert log.consistency_proof(5) == []
    assert log.verify_consistency(5, log.root(), [])
    # same size but a different root is not consistent
    assert not log.verify_consistency(5, _log_of(["x"] * 5).root(), [])


def test_consistency_rejects_out_of_range_sizes():
    log = _log(4)
    with pytest.raises(IndexError):
        log.consistency_proof(9)
    with pytest.raises(IndexError):
        log.consistency_proof(-1)
    assert log.verify_consistency(9, log.root(), []) is False
    assert log.verify_consistency(-1, log.root(), []) is False


def test_consistency_rejects_malformed_proof_data():
    log = _log(8)
    assert log.verify_consistency(4, "not-hex", log.consistency_proof(4)) is False
    assert log.verify_consistency(4, _log(4).root(), ["zzzz"]) is False


def test_consistency_rejects_truncated_and_padded_proofs():
    log = _log(8)
    old_root = _log(3).root()
    good = log.consistency_proof(3)
    assert log.verify_consistency(3, old_root, good)
    assert not log.verify_consistency(3, old_root, good[:-1])          # too short
    assert not log.verify_consistency(3, old_root, good + [good[0]])   # too long


def test_consistency_proof_is_logarithmic():
    """O(log n) is the point — the proof must not grow with the log."""
    log = _log(1024)
    assert len(log.consistency_proof(512)) <= 12


def test_consistency_rejects_a_proof_truncated_at_any_point():
    """Every step of the walk must fail closed when the proof runs out."""
    for n, m in ((8, 3), (8, 5), (16, 7), (13, 6), (11, 9)):
        log, old_root = _log(n), _log(m).root()
        full = log.consistency_proof(m)
        assert log.verify_consistency(m, old_root, full)
        for cut in range(len(full)):
            assert not log.verify_consistency(m, old_root, full[:cut]),                 f"n={n} m={m} accepted a proof truncated to {cut}"


def test_verify_consistency_guards_impossible_sizes():
    """Direct check of the internal guard: the public API cannot reach it."""
    log = _log(4)
    root = bytes.fromhex(log.root())
    assert _verify_consistency(9, 4, [], root, root) is False    # old bigger than new
    assert _verify_consistency(-1, 4, [], root, root) is False   # negative old size

