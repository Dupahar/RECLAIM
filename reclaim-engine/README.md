# RECLAIM Engine

**Reconciliation-Enabled Closed-Loop AI for Integrity & Money-recovery** — the deterministic core.

This repository is the **foundation** of RECLAIM. It is built bottom-up, and the bottom is deliberately **AI-free**: a system that moves money must first be *provably correct* with deterministic code. AI (exception resolution, recovery prediction) is layered on top only after this bedrock is exhaustively tested.

## Non-negotiable engineering rules (this project's contract)

1. **Correctness over speed.** Money math is exact and deterministic — never floating point.
2. **Test before moving on, then test again.** No layer advances until its tests are green and re-run.
3. **No fabrication.** Behaviour is proven by tests, not asserted in prose. Confidence and limitations are stated.
4. **Document every step.** See `docs/BUILD-LOG.md` and `docs/decisions/` (ADRs).
5. **Safe by construction.** Invariants (e.g. debits == credits, immutability, idempotency) are enforced in code, not by convention.

These principles are elaborated in the architecture document (`../RECLAIM-System-Architecture.md`) and the ADRs in `docs/decisions/`.

## Layout (src layout)

```
reclaim-engine/
  src/reclaim/            # the engine (importable package)
  tests/                  # pytest suite (mirrors src)
  docs/
    BUILD-LOG.md          # chronological log of what was built & tested
    decisions/            # Architecture Decision Records (ADRs)
  pyproject.toml          # project + pytest config
```

## Foundation build order

| Phase | Module | Guarantee |
|-------|--------|-----------|
| 1 | `money` | Decimal, currency-safe money; no float ever |
| 2 | `domain` | Canonical Transaction / Fees / LedgerEntry / LeakRecord |
| 3 | `ledger` | Immutable double-entry ledger; `debits == credits`; idempotent; replayable |
| 4 | `reconciliation` | Settlement decomposition + exact deterministic matching + leak detection |

## Running the tests

```bash
python -m pytest            # from reclaim-engine/
python -m pytest -v         # verbose
python -m pytest --cov=reclaim --cov-branch    # coverage (line + branch)
```

All tests must pass before any phase is considered complete. The suite runs at
**100% line + branch coverage**.

## Running the engine

End-to-end demo:

```bash
python -m reclaim.demo
```

Reconcile a real JSON batch from the command line (detection-only; safe, no side effects):

```bash
python -m reclaim examples/sample_batch.json           # human-readable report
python -m reclaim examples/sample_batch.json --json    # machine-readable summary
python -m reclaim --csv examples/sample_batch.csv      # CSV input (same validation as JSON)
python -m reclaim examples/sample_batch.json --store ./runstore              # persist ledger + audit
python -m reclaim examples/sample_batch.json --store ./runstore --key-file key.bin   # + sign audit root
```

`--store DIR` writes an append-only `ledger.jsonl` and `audit.jsonl` under `DIR`;
reloading them reproduces byte-identical balances and Merkle audit root. `--at
<ISO>` sets the audit-event timestamp (defaults to the first transaction's).

Verify a stored run later (tamper detection):

```bash
python -m reclaim --replay ./runstore                        # re-check balances + audit proofs
python -m reclaim --replay ./runstore --expect-root <HEX>    # confirm the audit root is unchanged
```

Exit code `0` = verified, `1` = verification failed (unbalanced / proof failure
/ root mismatch / corrupt data), `2` = usage error. Publish the audit root at
`--store` time and pass it to `--expect-root` later to prove nothing was altered.

With `--key-file`, `--store` writes a signed `audit.sig` and `--replay` verifies
it (HMAC-SHA256) — authenticity plus integrity, without needing to publish the
root out of band:

```bash
python -m reclaim --replay ./runstore --key-file key.bin   # verifies balances + proofs + signature
```

Or use the library directly:

```python
from reclaim.pipeline import run_reclaim
report = run_reclaim(settlements, bank_credits)         # optional: resolver=, recovery_engine=
print(report.summary())                                 # match rate / matched / recovered / residual
```

The batch JSON shape is documented in `src/reclaim/batch_io.py`; a working
example is in `examples/sample_batch.json`. Amounts **must** be strings (they
become exact `Money`, never float).
