# ADR-0001 — Foundation tech stack & correctness principles

**Status:** Accepted · **Date:** 2026-08-25

## Context
RECLAIM moves and reconciles money. The architecture (`../../RECLAIM-System-Architecture.md`) mandates a deterministic core that owns all money math, with AI layered above only as gated proposals. We must choose a foundation stack and correctness rules before writing domain code.

## Decision
1. **Language: Python (3.11+).** Rationale: the later layers (Splink record-linkage, contextual bandits, causal uplift) are Python-native, so a single language spans the stack; excellent testing ecosystem. *(Design decision.)*
2. **Money = `decimal.Decimal`, never `float`.** Binary floating point cannot represent decimal currency exactly (e.g. 0.1 + 0.2 != 0.3); using it for money violates goal **G1 (Correctness)**. All monetary values flow through a dedicated `Money` type. *(High confidence — established numerical fact.)*
3. **Plain `dataclasses` + explicit validation for the core, not a heavy framework.** Rationale: the foundation must be fully auditable and dependency-light; validation logic for money invariants is explicit and testable. Libraries (pydantic, etc.) may be adopted at the API boundary later. *(Design decision.)*
4. **`pytest`, src layout, tests mirror `src`.** Every module ships with exhaustive tests; a phase is not "done" until green and re-run.
5. **Immutability & idempotency enforced in code.** Ledger entries are immutable; postings are idempotent by key. Invariants are asserted, not assumed.

## Consequences
- Slightly more boilerplate than using a validation framework, accepted in exchange for an auditable, self-contained core.
- `Decimal` requires explicit rounding decisions (documented per operation).
- The foundation has **zero runtime dependencies**, so its correctness rests only on the standard library and our tests.

## Alternatives considered
- **float for money** — rejected (inexact; unacceptable for G1).
- **pydantic for core models** — deferred to the boundary; not needed for the deterministic core and would add a dependency to the bedrock.
- **Go/Rust for the ledger** (cf. TigerBeetle) — higher performance, but splits the stack from the Python ML layers; revisit at scale (architecture P2), not at foundation.
