# ADR-0012 — LLM exception resolver behind a testable ChatClient seam

**Status:** Accepted · **Date:** 2026-08-26

## Context
Phase 6 defined `GatedResolver` with deterministic fakes; the real integration
is a language model that adjudicates review-band candidates. We must wire in a
real LLM without (a) making the core depend on the `anthropic` SDK, (b) breaking
the "test before moving on / 100% coverage" discipline against a stochastic,
network-bound API, or (c) letting a model failure ever cause a wrong match.

## Decision
Add `LLMExceptionResolver`, which implements the `ExceptionResolver` protocol by
calling a **`ChatClient`** — a tiny `complete(system, user) -> str` seam.

- **Model access is injected.** Tests use deterministic fakes (`StaticChatClient`,
  `SequenceChatClient`, `RaisingChatClient`). The real backend,
  `build_anthropic_chat_client()`, is a thin factory (lazy `import anthropic`,
  `# pragma: no cover`) so the core stays dependency-free and the parsing/gating
  logic is 100%-testable offline.
- **Structured JSON verdict.** The resolver expects `{is_match, confidence,
  rationale}`; the real client constrains output with `output_config.format`
  (schema `VERDICT_SCHEMA`). `confidence` is parsed to `Decimal` (no float).
- **Default model `claude-opus-4-8`**, configurable.
- **Safe degradation (G6).** Any failure — client raises, non-JSON, missing/
  invalid field, out-of-range confidence — becomes a `ResolverError`, which the
  `GatedResolver` turns into `ESCALATE_HUMAN`. A model can cause an escalation,
  never a wrong match. It composes unchanged with the Phase-6 gate
  (self-consistency + confidence threshold + adversarial verifier).

## Consequences
- The real LLM's stochasticity is contained by the gate; its failures by safe
  degradation. Swapping providers = a new `ChatClient`, no core change.
- Running against a live model requires the `anthropic` package + credentials;
  the engine's tests and `python -m reclaim`/`reclaim.demo` never need them.
- Prompt/verdict shape is fixed here; richer signals (narration text, evidence)
  can be added to the prompt without changing the interface.

## Alternatives considered
- **Depend on `anthropic` directly in the resolver** — rejected (couples the
  core to a network SDK; untestable offline; violates dependency-light ADR-0001).
- **Free-form model output parsed heuristically** — rejected in favour of
  structured JSON + strict validation (predictable, safe-degrading).
- **Trust the model's confidence without the gate** — rejected; the gate's
  consensus + verifier is what makes an LLM verdict safe to act on.
