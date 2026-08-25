# ADR-0010 — Record signing with HMAC over the audit root

**Status:** Accepted · **Date:** 2026-08-26

## Context
The Merkle audit root proves a run was not altered (integrity). It does not
prove *who* produced it (authenticity), and `--expect-root` (ADR-0009) requires
holding the root out of band. We want authenticity and a signature that can be
stored with the run.

## Decision
Sign the audit root with **HMAC-SHA256** (stdlib `hmac`).
- `signing.sign_root/verify_root` and `signed_root_record/verify_signed_record`
  (an attestation dict `{algo, root, signature}`).
- Verification is constant-time (`hmac.compare_digest`) and returns `False`
  (never raises) for a malformed signature; it raises only for an invalid key.
- CLI `--key-file PATH`: on `--store`, write `audit.sig` (the signed root); on
  `--replay`, recompute the root and verify the signature against it.

Because forging a valid signature over a modified root requires the key, the
`audit.sig` file can live *alongside* the data — a tamperer without the key
cannot re-sign the changed root. This is stronger than a co-located
`--expect-root` and needs no out-of-band publication.

## Consequences
- Authenticity + integrity in one check, verifiable offline with the key.
- **Honest limitation:** HMAC is *symmetric* — the verifier needs the same
  secret key, so it does not give public verifiability. The upgrade is an
  asymmetric scheme (e.g. Ed25519), which requires a crypto dependency
  (out of scope for the dependency-light foundation) — documented, not hidden.
- Keys are read from a file (`--key-file`), never passed on the command line
  (which would leak via process listings).

## Alternatives considered
- **Ed25519 / RSA now** — better (public verification) but adds a heavy crypto
  dependency; deferred with a clear upgrade path.
- **Signing every record** — unnecessary; the Merkle root already commits to all
  records, so one signature over the root covers the whole log.
- **Key on the CLI / in the store dir** — rejected (leaks / co-located secret is
  no protection).
