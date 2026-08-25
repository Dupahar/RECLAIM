"""Record signing — authenticity on top of the Merkle integrity root.

The Merkle audit root proves a run was *not altered*; a signature over that root
proves *who* attested to it. We sign the audit root with **HMAC-SHA256** (stdlib
``hmac``): whoever holds the secret key can produce and verify the signature.

Because forging a valid signature over a modified root requires the key, the
signature is a strong tamper-evidence anchor even when stored *alongside* the
data — a tamperer without the key cannot re-sign the changed root.

Honest limitation (see ADR-0010): HMAC is *symmetric* — the verifier needs the
same secret key. Public verifiability (anyone can check, no shared secret) would
use an asymmetric scheme (e.g. Ed25519), which needs a crypto dependency and is
the documented upgrade path.
"""
from __future__ import annotations

import hashlib
import hmac

ALGO = "HMAC-SHA256"


class SigningError(Exception):
    """Raised for invalid keys."""


def _check_key(key: bytes) -> None:
    if not isinstance(key, (bytes, bytearray)) or len(key) == 0:
        raise SigningError("signing key must be non-empty bytes")


def sign(message: bytes, key: bytes) -> str:
    """HMAC-SHA256 of message under key, as hex."""
    _check_key(key)
    if not isinstance(message, (bytes, bytearray)):
        raise SigningError("message must be bytes")
    return hmac.new(bytes(key), bytes(message), hashlib.sha256).hexdigest()


def verify(message: bytes, signature_hex: str, key: bytes) -> bool:
    """Constant-time verification. Returns False (never raises) for a bad
    signature; raises only for an invalid key."""
    expected = sign(message, key)
    if not isinstance(signature_hex, str):
        return False
    try:
        return hmac.compare_digest(expected, signature_hex)
    except TypeError:
        return False


def sign_root(root_hex: str, key: bytes) -> str:
    return sign(root_hex.encode("ascii"), key)


def verify_root(root_hex: str, signature_hex: str, key: bytes) -> bool:
    return verify(root_hex.encode("ascii"), signature_hex, key)


def signed_root_record(root_hex: str, key: bytes) -> dict:
    """A portable, JSON-serializable attestation of an audit root."""
    return {"algo": ALGO, "root": root_hex, "signature": sign_root(root_hex, key)}


def verify_signed_record(record: dict, key: bytes, actual_root: str) -> bool:
    """True iff `record` is a valid signature, by `key`, of `actual_root`."""
    if not isinstance(record, dict):
        return False
    if record.get("algo") != ALGO:
        return False
    if record.get("root") != actual_root:      # the attestation must be for this exact root
        return False
    return verify_root(actual_root, record.get("signature", ""), key)
