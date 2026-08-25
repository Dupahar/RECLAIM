"""Merkle transparency audit log — tamper-evident record of decisions.

RECLAIM's integrity pillar (architecture Section 9). Every decision — a match,
a recovery action, an escalation, a *non*-action — is appended to an
append-only Merkle log. Following the Certificate-Transparency design
(RFC 6962; Crosby-Wallach history tree):

- **Domain-separated hashing.** ``leaf = H(0x00 || data)``,
  ``node = H(0x01 || left || right)`` (SHA-256). The prefixes prevent
  second-preimage / leaf-vs-node confusion attacks.
- **Merkle Tree Hash (MTH).** The root commits to the entire ordered log; any
  edit or deletion of a past entry changes the root and is detectable.
- **Inclusion proofs.** O(log n) audit paths prove a specific event is in the
  log under a given root, without revealing the rest.
- **Append-only checking.** ``root_at(size)`` recomputes the root over a prefix,
  so a previously-published root can be re-verified as an unchanged prefix
  (a simple, correct consistency check; RFC 6962's O(log n) consistency proof
  is the scale-up).

Dependency-light: standard-library ``hashlib``/``json`` only. Deterministic.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _leaf_hash(data: bytes) -> bytes:
    return _sha256(_LEAF_PREFIX + data)


def _node_hash(left: bytes, right: bytes) -> bytes:
    return _sha256(_NODE_PREFIX + left + right)


def _largest_power_of_two_less_than(n: int) -> int:
    """Largest power of two strictly less than n (n >= 2)."""
    k = 1
    while k * 2 < n:
        k *= 2
    return k


def _mth(leaf_hashes: list[bytes]) -> bytes:
    """Merkle Tree Hash of a list of leaf hashes (RFC 6962)."""
    n = len(leaf_hashes)
    if n == 0:
        return _sha256(b"")          # hash of the empty string, per RFC 6962
    if n == 1:
        return leaf_hashes[0]
    k = _largest_power_of_two_less_than(n)
    return _node_hash(_mth(leaf_hashes[:k]), _mth(leaf_hashes[k:]))


def _audit_path(leaf_hashes: list[bytes], m: int) -> list[tuple[bytes, bool]]:
    """Inclusion proof for index m: list of (sibling_hash, sibling_is_left),
    ordered from the deepest sibling up to the root's child."""
    n = len(leaf_hashes)
    if n == 1:
        return []
    k = _largest_power_of_two_less_than(n)
    if m < k:
        # our leaf is in the left subtree; the right subtree root is our sibling (on the right)
        return _audit_path(leaf_hashes[:k], m) + [(_mth(leaf_hashes[k:]), False)]
    # our leaf is in the right subtree; the left subtree root is our sibling (on the left)
    return _audit_path(leaf_hashes[k:], m - k) + [(_mth(leaf_hashes[:k]), True)]


def _verify_path(leaf_hash: bytes, path: list[tuple[bytes, bool]], root: bytes) -> bool:
    r = leaf_hash
    for sibling, sibling_is_left in path:
        r = _node_hash(sibling, r) if sibling_is_left else _node_hash(r, sibling)
    return r == root


@dataclass(frozen=True)
class AuditEvent:
    kind: str
    at: datetime
    detail: Mapping[str, str] = field(default_factory=dict)

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization used for hashing (stable key order)."""
        obj = {"kind": self.kind, "at": self.at.isoformat(),
               "detail": {k: self.detail[k] for k in sorted(self.detail)}}
        return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


class MerkleAuditLog:
    """An append-only, tamper-evident log of audit events."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._leaves: list[bytes] = []

    def append(self, event: AuditEvent) -> int:
        if not isinstance(event, AuditEvent):
            raise TypeError("append expects an AuditEvent")
        self._events.append(event)
        self._leaves.append(_leaf_hash(event.canonical_bytes()))
        return len(self._events) - 1

    @property
    def size(self) -> int:
        return len(self._events)

    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    def root(self) -> str:
        return _mth(self._leaves).hex()

    def root_at(self, size: int) -> str:
        """Root over the first `size` entries — for append-only/prefix checks."""
        if not (0 <= size <= self.size):
            raise IndexError(f"size {size} out of range 0..{self.size}")
        return _mth(self._leaves[:size]).hex()

    def inclusion_proof(self, index: int) -> list[tuple[str, bool]]:
        if not (0 <= index < self.size):
            raise IndexError(f"index {index} out of range 0..{self.size - 1}")
        return [(sib.hex(), is_left) for sib, is_left in _audit_path(self._leaves, index)]

    def verify_inclusion(self, event: AuditEvent, index: int,
                         proof: list[tuple[str, bool]], root_hex: str) -> bool:
        """Verify `event` sits at `index` in a tree with the given root."""
        if not (0 <= index < self.size):
            return False
        leaf = _leaf_hash(event.canonical_bytes())
        path = [(bytes.fromhex(h), is_left) for h, is_left in proof]
        return _verify_path(leaf, path, bytes.fromhex(root_hex))
