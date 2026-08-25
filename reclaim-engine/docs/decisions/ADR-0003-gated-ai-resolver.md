# ADR-0003 — The AI enters gated, behind an interface, degrading to human

**Status:** Accepted · **Date:** 2026-08-25

## Context
Phase 6 is the first layer where a model (LLM) makes a judgement: adjudicating
the Phase-5 review band (ambiguous candidate matches). Money is at stake and
LLMs are stochastic and fallible, so we must decide how AI output is trusted
and how this layer stays testable despite non-determinism.

## Decision
1. **Behind an interface.** Models implement the `ExceptionResolver` protocol
   (`assess(...) -> Assessment`). Production wires an LLM; tests use
   deterministic fakes (`StaticResolver`, `SequenceResolver`, `RaisingResolver`).
   The gating logic is therefore 100%-unit-testable with no real model.
2. **Gated, not obeyed.** `GatedResolver` applies, in order: **self-consistency**
   (sample N times, require a strict majority), a **confidence gate**
   (conservative = weakest supporting sample must clear `accept_confidence`),
   and an **adversarial verifier** (a second opinion prompted to refute). Only
   consensus + confident + unrefuted ⇒ `CONFIRMED_MATCH`.
3. **Safe degradation (goal G6).** Any uncertainty — no majority, low
   confidence, verifier refutation, or *any* `ResolverError` (e.g. API failure)
   — yields `ESCALATE_HUMAN`. Clear negative consensus yields `REJECTED`. The
   layer never silently accepts/rejects on a guess.
4. **It does not move money.** A `CONFIRMED_MATCH` is an *input* to a later,
   separately-gated ledger posting — confirmation here is advisory, not an
   action.
5. **Auditable.** Every outcome carries its samples (and the verifier opinion)
   for the audit trail.

## Consequences
- The gate is intentionally conservative (min-confidence aggregation, strict
  majority) — it will escalate more than a lenient design, trading throughput
  for safety. Appropriate for money; thresholds are tunable via `ResolverConfig`.
- A real LLM resolver is a future drop-in implementing the protocol; its
  stochasticity is contained by self-consistency + verifier, and its failures by
  safe degradation. No core logic changes when it is added.
- `Assessment.confidence` is `Decimal` (consistent no-float discipline), even
  though it is a probability, to keep comparisons exact and deterministic.

## Alternatives considered
- **Trust a single LLM call** — rejected (no consistency check, no safe
  degradation; unacceptable for money).
- **Let the resolver auto-post confirmed matches** — rejected (violates
  bounded-autonomy; posting must be a separate gated step).
