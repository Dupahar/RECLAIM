"""Phase 14 tests — record signing (HMAC over the audit root)."""
import pytest

from reclaim.signing import (
    ALGO,
    SigningError,
    sign,
    sign_root,
    signed_root_record,
    verify,
    verify_root,
    verify_signed_record,
)

KEY = b"super-secret-key"
ROOT = "2e01c7d00ad76c124ad0d906a78caf5f6b524aa8aacb4ea0fb70bb56027482f6"


# --------------------------------------------------------------------------
# Core sign / verify
# --------------------------------------------------------------------------
def test_sign_verify_roundtrip():
    sig = sign(b"hello", KEY)
    assert verify(b"hello", sig, KEY) is True


def test_sign_is_deterministic():
    assert sign(b"x", KEY) == sign(b"x", KEY)


def test_wrong_key_fails():
    sig = sign(b"hello", KEY)
    assert verify(b"hello", sig, b"other-key") is False


def test_tampered_message_fails():
    sig = sign(b"hello", KEY)
    assert verify(b"HELLO", sig, KEY) is False


def test_bad_signature_is_false_not_error():
    assert verify(b"hello", "not-hex-or-wrong", KEY) is False
    assert verify(b"hello", 12345, KEY) is False  # type: ignore[arg-type]
    # a non-ASCII string makes compare_digest raise TypeError -> handled as False
    assert verify(b"hello", "café-not-ascii", KEY) is False


def test_empty_key_rejected():
    with pytest.raises(SigningError):
        sign(b"m", b"")
    with pytest.raises(SigningError):
        sign(b"m", "notbytes")  # type: ignore[arg-type]


def test_non_bytes_message_rejected():
    with pytest.raises(SigningError):
        sign("not-bytes", KEY)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Root signing
# --------------------------------------------------------------------------
def test_sign_verify_root():
    sig = sign_root(ROOT, KEY)
    assert verify_root(ROOT, sig, KEY) is True
    assert verify_root("00" * 32, sig, KEY) is False    # different root


# --------------------------------------------------------------------------
# Signed record
# --------------------------------------------------------------------------
def test_signed_record_verifies():
    rec = signed_root_record(ROOT, KEY)
    assert rec["algo"] == ALGO and rec["root"] == ROOT
    assert verify_signed_record(rec, KEY, ROOT) is True


def test_signed_record_rejects_root_mismatch():
    rec = signed_root_record(ROOT, KEY)
    assert verify_signed_record(rec, KEY, "ff" * 32) is False   # actual root differs


def test_signed_record_rejects_wrong_key():
    rec = signed_root_record(ROOT, KEY)
    assert verify_signed_record(rec, b"nope", ROOT) is False


def test_signed_record_rejects_bad_algo_or_shape():
    assert verify_signed_record({"algo": "MD5", "root": ROOT, "signature": "x"}, KEY, ROOT) is False
    assert verify_signed_record("not-a-dict", KEY, ROOT) is False  # type: ignore[arg-type]


def test_signed_record_detects_tampered_signature():
    rec = signed_root_record(ROOT, KEY)
    rec["signature"] = "00" * 32
    assert verify_signed_record(rec, KEY, ROOT) is False
