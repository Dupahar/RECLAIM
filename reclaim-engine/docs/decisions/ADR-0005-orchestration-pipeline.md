# ADR-0005 — Orchestration is a pure, deterministic, dependency-injected pipeline

**Status:** Accepted · **Date:** 2026-08-25

## Context
The modules (reconcile / probabilistic / resolver / recovery / ledger) are each
tested in isolation. We need one entry point that runs the full loop over a
batch and reports honestly — without sacrificing determinism or testability,
and without hard-wiring the two external integrations (LLM, payment rail).

## Decision
`run_reclaim(...)` composes the layers in a fixed order and returns a frozen
`RunReport`:
1. exact reconcile → 2. probabilistic on the residual → 3. gated resolver on the
review band (**optional**, injected) → 4. bounded recovery on recoverable leaks
(**optional**, injected) → 5. post reconciled + recovered money to a fresh
`Ledger`.

- **Dependency injection.** `resolver` and `recovery_engine` are optional
  parameters. Absent resolver → review candidates are reported as
  `pending_review` (needs human). Absent recovery engine → recoverable leaks
  simply remain leaks. This lets the pipeline run in any capability tier and
  keeps tests hermetic.
- **Determinism.** No `now()`; `base_time` is supplied (and required when a
  recovery engine is present). Same inputs ⇒ same `RunReport` (verified).
- **Honest reporting.** The report exposes the three numbers (match rate /
  matched / recovered), the **residual exception list** (leaks resolved by *no*
  path), plus AI and recovery audit collections and the balanced ledger.

## Consequences
- The pipeline owns only *wiring, reporting, and posting*; all domain logic
  stays in the tested layers. Its own branches are covered 100%.
- Residual computation is explicit and conservative: a leak is dropped from the
  residual only if positively resolved (fuzzy/AI match for missing/unexpected,
  or a successful recovery for a shortfall) — never assumed.
- `reclaim.demo` provides a runnable, reproducible end-to-end demonstration
  (`python -m reclaim.demo`) using deterministic stand-ins for the LLM and rail.

## Alternatives considered
- **A monolithic reconcile-and-recover function** — rejected (couples layers,
  harder to test, no capability tiers).
- **Mandatory resolver/recovery** — rejected (forces external dependencies into
  every run and every test).
