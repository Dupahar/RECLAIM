# ADR-0006 — I/O boundary: strict hand-validation, and a detection-only CLI

**Status:** Accepted · **Date:** 2026-08-25

## Context
To run on real data, RECLAIM needs a front door that turns external JSON into
canonical, validated domain objects, plus a command-line entry point. Two
questions: how to validate input, and what the CLI is allowed to do.

## Decision
1. **Amounts must arrive as strings; never parsed as float.** `batch_io` requires
   `gross_amount` and every fee to be JSON strings and routes them through
   `Money.of`. A JSON number (float) is rejected. This extends the no-float
   correctness rule (ADR-0001) to the I/O boundary — where precision is most
   often lost.
2. **Strict, hand-written validation with clear errors.** Missing/typed fields,
   bad ISO timestamps, bad amounts, non-string refs, and domain-invariant
   violations (e.g. a negative fee) all surface as a single `BatchLoadError`
   with a located message (`settlements[2].fees.mdr: ...`). Kept dependency-free
   and fully testable; a validation library (pydantic) remains an option but is
   not needed for this shape.
3. **The CLI runs detection only.** `python -m reclaim <batch.json>` runs the
   exact + probabilistic pipeline with **no** AI resolver and **no** recovery
   engine, because those are external integrations (LLM, payment rail). Review-
   band candidates and recoverable leaks are *reported* (pending review /
   residual), never acted on or guessed. `--json` emits a machine-readable
   summary.

## Consequences
- Real settlement/bank exports must be shaped into the documented JSON (a thin
  adapter's job). The architecture's LLM-assisted extraction for messy formats
  would produce this same canonical JSON, behind this boundary.
- The CLI is safe to run anywhere (no side effects, no external calls). Enabling
  recovery/AI is a deliberate, separately-wired step (as in `reclaim.demo`).
- Coverage omits the 2-line `__main__` shim (its logic is `cli.main`, tested to
  100%); running `python -m reclaim examples/sample_batch.json` is verified
  manually and via `test_cli`.

## Alternatives considered
- **Accept JSON numbers for amounts** — rejected (float precision loss).
- **A CLI that performs recovery/AI by default** — rejected (side effects and
  external dependencies in a plain report command).
