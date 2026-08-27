# ADR-0015 — A replay must be anchored, or it proves nothing

**Status:** Accepted · 2026-08-28 · supersedes part of [ADR-0009](ADR-0009-replay-verification.md)
**Context phase:** post-Sprint-1 review

## Context

`verify_stores()` recomputes the Merkle root from the stored events and checks
every inclusion proof against **that recomputed root**. The check is therefore
self-referential: delete or edit stored events and the log simply re-roots
itself. The surviving events still prove inclusion in the smaller tree.

Demonstrated: deleting 2 of 4 audit events from a stored run and replaying it
reported `VERIFIED`, `inclusion proofs: ok`, **exit 0**.

Tamper detection only ever came from `--expect-root` (or the `--key-file` HMAC,
which signs the original root). Both were optional, and `--replay DIR` on its
own — the form used in our own README — was the unsafe default.

## Decision

Make an anchor mandatory, and supply one automatically.

1. **`--store` publishes the root.** It writes `root.txt` containing the audit
   root next to `ledger.jsonl` / `audit.jsonl`.
2. **`--replay` resolves an anchor, in order:** explicit `--expect-root` →
   `root.txt` in the run directory → an HMAC signature via `--key-file`.
3. **No anchor ⇒ refuse.** Print `UNANCHORED replay - tampering cannot be
   detected`, explain why, and exit `2`. The command does not report a verdict
   it cannot justify.
4. The verification output names its anchor, so a reader can see *what* the run
   was checked against, not just that it passed.

## Why not just warn

A warning that still exits `0` is invisible to the thing that matters — the
scripted check in CI. The failure mode we are closing is precisely "an automated
verifier reports success on a gutted log". Refusing is the only outcome that
changes that.

## Why `root.txt` inside the run directory

Convenience, not security. An attacker who can rewrite `audit.jsonl` can rewrite
`root.txt` beside it. Its job is to make the *anchored* path the effortless
default so nobody reaches for the unanchored one. **For real tamper evidence the
root must also live somewhere the run directory's owner cannot silently edit** —
commit it, file it on a ticket, or sign it with `--key-file` and hold the key
elsewhere. This is stated in the README and the RUNBOOK.

## Consequences

- Breaking for one case: `--replay DIR` against a store created before this
  change, with no key, now exits `2` instead of `0`. The fix is to pass
  `--expect-root` with the root that was published at store time.
- The signature path is unchanged and still detects the same tampering on its
  own — `--key-file` alone remains a valid anchor.

## Tested by

`tests/test_cli.py` — `test_store_publishes_root_file`,
`test_replay_auto_anchors_on_root_file`,
`test_replay_auto_anchor_catches_deleted_audit_events` (the regression),
`test_replay_refuses_when_unanchored`, `test_replay_refuses_on_empty_root_file`,
`test_explicit_expect_root_overrides_root_file`,
`test_key_file_alone_anchors_replay`,
`test_key_file_alone_catches_deleted_audit_events`.
