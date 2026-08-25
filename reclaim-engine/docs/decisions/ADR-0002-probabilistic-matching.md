# ADR-0002 — Probabilistic matching: a transparent scorer, not a black box

**Status:** Accepted · **Date:** 2026-08-25

## Context
Above the exact reconciliation gate (Phase 4), we need a "second brain" that
recovers matches the exact matcher misses (timing offsets, garbled UTRs,
alternate references). The architecture cites Splink / Fellegi-Sunter for this.
We must decide *how* to build it at the foundation stage.

## Decision
Build a **hand-written, transparent, deterministic** Fellegi-Sunter-style
weighted scorer in `probabilistic.py`, rather than pulling in Splink or a
learned model now.

- Scores are `Decimal` in [0,1] (never float), from explicit per-field
  agreement scorers (amount closeness, date proximity, shared reference) with
  documented weights that sum to 1.
- Three decision bands: `auto` (>= match_threshold), `review`
  (>= review_threshold) for the future AI/human resolver, and `residual`.
- Greedy one-to-one assignment with fully deterministic ordering (score desc,
  then id asc).

## Rationale
1. **Explainability & calibration matter for money.** A transparent weighted
   score can be audited, unit-tested to 100%, and calibrated by hand — a learned
   model or a heavyweight dependency cannot, at this stage.
2. **Determinism.** Decimal + deterministic tie-breaking keeps the layer
   replayable (goal **G4**), consistent with the rest of the foundation.
3. **Dependency-light foundation** (ADR-0001). Splink needs DuckDB/Spark; that
   is a scale-up concern, not a foundation one.
4. **It never auto-acts.** The layer *proposes* scored matches; acting on a
   review-band candidate is a later, gated decision. The exact gate always
   runs first and is never overridden.

## Consequences
- The scorer is simpler than a full Fellegi-Sunter EM model (fixed weights, not
  EM-estimated m/u probabilities). Accepted: interpretability now, learned
  weights later once we have labelled outcome data.
- Reference agreement is exact-field-match only (no fuzzy string similarity yet)
  — a deliberate, testable starting point; fuzzy string/亂 similarity can be
  added as an additional field scorer without changing the interface.

## Alternatives considered
- **Splink / dedupe / Zingg now** — deferred to scale (heavy deps, harder to
  unit-test/calibrate at the foundation).
- **LLM-based matcher** — this is the *next* layer (the AI exception resolver),
  which sits above this deterministic scorer and is gated by it; not a
  replacement for it.
