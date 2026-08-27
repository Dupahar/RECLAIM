# ADR-0019 — Publish a head history and verify consistency, not just inclusion

**Status:** Accepted · 2026-08-28 · completes [ADR-0007](ADR-0007-merkle-audit-log.md), extends [ADR-0015](ADR-0015-anchored-replay.md)

## Context

ADR-0015 closed the unanchored-replay hole by publishing the audit root to
`root.txt` and refusing to verify without an anchor. That defends against a log
edited *after* publication. It does not defend against a log edited and then
*re-published* — an attacker who can rewrite `audit.jsonl` can rewrite the root
beside it, and every inclusion proof will still pass, because a Merkle log
recomputes a perfectly valid root of whatever entries survive.

Architecture §9.1 always specified "O(log n) inclusion **and consistency**
proofs". Only the first half was built.

## Decision

Implement RFC 6962 consistency proofs (`consistency_proof` /
`verify_consistency`), and give the CLI something to check them against.

`--store` appends the tree head it published — `<size> <root>` — to an
append-only `roots.log`. `--replay` then verifies that the current log is an
append-only extension of **every** head ever published, and reports
`append-only: ok (vs N published head/s)`.

The distinction the two proofs draw:

- an **inclusion** proof answers "is this event in a tree with root R?"
- a **consistency** proof answers "is R's history the same history I saw before?"

Only the second catches a rewrite.

## Consequences

- An attacker must now also rewrite `roots.log`, and every copy of it that was
  ever published elsewhere. The more heads that leave the machine, the harder
  the forgery. This is the Certificate Transparency model and it has the same
  limitation: an anchor kept only next to the data it anchors is a convenience,
  not a guarantee.
- Fixed a related bug this surfaced: `root.txt` and the signature were written
  from the *run's* audit log rather than the *store's*. Identical for a fresh
  directory, wrong the moment a second batch is persisted into the same one.
  Both now publish the store head.
- `verify_stores` gained `prior_heads`; `--replay` stays silent about
  append-only when no heads exist, rather than reporting a vacuous "ok".

## Tested by

`tests/test_audit.py` — exhaustive verification over every `(old_size, new_size)`
pair up to 33 entries; rewrite, deletion and reorder all rejected;
`test_inclusion_alone_cannot_catch_that_rewrite` states the gap as an assertion;
proofs truncated at *every* position are rejected; proof length is O(log n).
`tests/test_cli.py::test_replay_catches_a_rewrite_hidden_behind_a_refreshed_root`
is the end-to-end version — `root matches: True`, `inclusion proofs: ok`,
`append-only: FAILED`, exit 1.
