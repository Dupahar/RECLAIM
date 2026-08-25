"""LLM-backed exception resolver — a real model behind the gated interface.

Implements the Phase-6 ``ExceptionResolver`` protocol by asking a language model
whether a settlement and a bank credit are the same underlying money. The model
is reached through a small ``ChatClient`` seam so that:

- the prompt-building, JSON-verdict parsing, and **safe-degradation** logic are
  fully unit-tested with deterministic fakes (no network, no stochasticity), and
- a real Anthropic-backed client is a thin drop-in (``build_anthropic_chat_client``).

**Safe degradation (goal G6):** any failure — the client raising, non-JSON
output, missing/invalid fields, out-of-range confidence — is turned into a
``ResolverError``, which the ``GatedResolver`` treats as *escalate to a human*.
The model can never cause a wrong match; at worst it causes an escalation.

Model default is ``claude-opus-4-8`` and the real client uses structured JSON
output (``output_config.format``) so the response is schema-constrained.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

from .resolver import Assessment, ResolverError

DEFAULT_MODEL = "claude-opus-4-8"

_SYSTEM = (
    "You are a payments reconciliation adjudicator. Decide whether a settlement "
    "payout and a bank credit represent the SAME underlying money. Consider "
    "amount, timing, and any references. Respond with ONLY a JSON object of the "
    'form {"is_match": <bool>, "confidence": <number 0..1>, "rationale": '
    "<short string>}. Do not include any other text."
)

# JSON schema for the real client's structured-output constraint.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_match": {"type": "boolean"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["is_match", "confidence", "rationale"],
    "additionalProperties": False,
}


@runtime_checkable
class ChatClient(Protocol):
    """The seam a language-model backend implements: text in, text out."""

    def complete(self, system: str, user: str) -> str:  # pragma: no cover - protocol
        ...


def _build_user_prompt(settlement, bank, prior_score: Decimal) -> str:
    s_refs = settlement.refs
    b_refs = bank.refs
    return (
        "Candidate match (probabilistic prior score "
        f"{prior_score}):\n"
        f"SETTLEMENT id={settlement.id} expected_payout={settlement.net_amount} "
        f"ts={settlement.ts.isoformat()} utr={s_refs.utr} order_id={s_refs.order_id}\n"
        f"BANK CREDIT id={bank.id} amount={bank.gross_amount} "
        f"ts={bank.ts.isoformat()} utr={b_refs.utr} order_id={b_refs.order_id}\n"
        "Are these the same money?"
    )


def _parse_verdict(text: str) -> Assessment:
    """Parse a model's JSON verdict into an Assessment, or raise ResolverError."""
    if not isinstance(text, str):
        raise ResolverError("model response was not text")
    cleaned = text.strip()
    # tolerate ```json ... ``` fences some models add
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ResolverError(f"model did not return valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ResolverError("model JSON was not an object")
    for key in ("is_match", "confidence", "rationale"):
        if key not in obj:
            raise ResolverError(f"model JSON missing '{key}'")
    if not isinstance(obj["is_match"], bool):
        raise ResolverError("'is_match' must be a boolean")
    try:
        confidence = Decimal(str(obj["confidence"]))
    except (InvalidOperation, ValueError) as exc:
        raise ResolverError(f"invalid confidence: {obj['confidence']!r}") from exc
    # Assessment enforces confidence in [0,1] and raises ResolverError otherwise.
    return Assessment(is_match=obj["is_match"], confidence=confidence,
                      rationale=str(obj["rationale"]))


@dataclass
class LLMExceptionResolver:
    """An ExceptionResolver backed by a ChatClient (real LLM or a fake)."""

    client: ChatClient
    model: str = DEFAULT_MODEL  # informational; the real client is configured separately

    def assess(self, settlement, bank, prior_score: Decimal) -> Assessment:
        user = _build_user_prompt(settlement, bank, prior_score)
        try:
            text = self.client.complete(_SYSTEM, user)
        except ResolverError:
            raise
        except Exception as exc:  # any client/transport failure -> escalate, never guess
            raise ResolverError(f"chat client error: {exc}") from exc
        return _parse_verdict(text)


# --------------------------------------------------------------------------
# Deterministic fake clients — for tests and offline use.
# --------------------------------------------------------------------------
@dataclass
class StaticChatClient:
    """Always returns the same text."""

    text: str

    def complete(self, system: str, user: str) -> str:
        return self.text


@dataclass
class SequenceChatClient:
    """Returns pre-set texts in order (to simulate sampling variation)."""

    texts: list = field(default_factory=list)
    _i: int = 0

    def complete(self, system: str, user: str) -> str:
        if self._i >= len(self.texts):
            raise ResolverError("SequenceChatClient exhausted")
        t = self.texts[self._i]
        self._i += 1
        return t


@dataclass
class RaisingChatClient:
    """Always raises — models an LLM/API failure to test safe degradation."""

    def complete(self, system: str, user: str) -> str:
        raise RuntimeError("simulated LLM API failure")


def build_anthropic_chat_client(model: str = DEFAULT_MODEL, api_key: str | None = None):  # pragma: no cover - needs network/SDK/key
    """Return a ChatClient backed by the Anthropic Messages API.

    Thin, network-dependent factory (not unit-tested). Uses structured JSON
    output so the response conforms to VERDICT_SCHEMA. Requires the ``anthropic``
    package and credentials in the environment.
    """
    import anthropic  # lazy import — keep the core dependency-free

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    class _AnthropicChatClient:
        def complete(self, system: str, user: str) -> str:
            resp = client.messages.create(
                model=model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
            )
            return next(b.text for b in resp.content if b.type == "text")

    return _AnthropicChatClient()
