# Contributing to RECLAIM

Thanks for your interest in RECLAIM. This project moves money, so correctness and
test discipline are non-negotiable.

## Ground rules

- **Money is `Decimal`, never `float`** — everywhere, including at the I/O boundary.
- **Test before you merge.** Every change ships with tests; the suite must stay at
  **100% line + branch coverage**.
- **Invariants are enforced in code**, not by convention (e.g. `debits == credits`,
  immutability, idempotency, safe degradation to a human).
- **Determinism.** No `datetime.now()` / randomness inside the engine — timestamps and
  keys are supplied by the caller so runs are reproducible and replayable.

## Development

```bash
cd reclaim-engine
python -m pytest --cov=reclaim --cov-branch --cov-report=term-missing
```

The build must be green and at 100% coverage before a change is considered done.

## Workflow

1. Create a feature branch.
2. Add code **and** tests; run the full suite with branch coverage.
3. Document any significant design decision as an ADR in `reclaim-engine/docs/decisions/`.
4. Open a pull request describing the change and its test evidence.

## Design decisions

Architecture Decision Records live in `reclaim-engine/docs/decisions/`. If you change
a core invariant or introduce a new integration, add or update the relevant ADR.
