"""Phase 16 tests — LLM-backed exception resolver (with deterministic fakes)."""
import json
from datetime import datetime
from decimal import Decimal

import pytest

from reclaim.money import Money
from reclaim.domain import Source, Transaction, TransactionRefs
from reclaim.probabilistic import ScoredMatch
from reclaim.resolver import Decision, GatedResolver, ResolverConfig, ResolverError
from reclaim.llm_resolver import (
    DEFAULT_MODEL,
    LLMExceptionResolver,
    RaisingChatClient,
    SequenceChatClient,
    StaticChatClient,
    _parse_verdict,
)

TS = datetime(2026, 8, 26, 9, 0, 0)


def _candidate(score="0.7000"):
    s = Transaction(id="s1", source=Source.SETTLEMENT, gross_amount=Money.of("100.00", "INR"),
                    ts=TS, refs=TransactionRefs(utr="U1"))
    b = Transaction(id="b1", source=Source.BANK, gross_amount=Money.of("100.00", "INR"),
                    ts=TS, refs=TransactionRefs(utr="U1X"))
    return ScoredMatch(settlement=s, bank=b, score=Decimal(score), band="review")


def _assess(text, score="0.7000"):
    cand = _candidate(score)
    return LLMExceptionResolver(StaticChatClient(text)).assess(cand.settlement, cand.bank, cand.score)


def _json(is_match, confidence, rationale="ok"):
    return json.dumps({"is_match": is_match, "confidence": confidence, "rationale": rationale})


# --------------------------------------------------------------------------
# Parsing a good verdict
# --------------------------------------------------------------------------
def test_valid_verdict_parsed():
    a = _assess(_json(True, 0.9, "amount and date align"))
    assert a.is_match is True
    assert a.confidence == Decimal("0.9")
    assert a.rationale == "amount and date align"


def test_confidence_int_becomes_decimal():
    a = _assess(_json(True, 1, "certain"))
    assert a.confidence == Decimal("1")


def test_json_in_code_fence_is_tolerated():
    fenced = "```json\n" + _json(False, 0.2) + "\n```"
    a = _assess(fenced)
    assert a.is_match is False and a.confidence == Decimal("0.2")


def test_plain_code_fence_without_json_tag():
    fenced = "```\n" + _json(True, 0.8) + "\n```"
    a = _assess(fenced)
    assert a.is_match is True and a.confidence == Decimal("0.8")


def test_client_raising_resolver_error_propagates():
    # a ChatClient that raises ResolverError directly (e.g. exhausted sequence)
    cand = _candidate()
    resolver = LLMExceptionResolver(SequenceChatClient([]))  # empty -> raises ResolverError
    with pytest.raises(ResolverError):
        resolver.assess(cand.settlement, cand.bank, cand.score)


def test_default_model_constant():
    assert DEFAULT_MODEL == "claude-opus-4-8"
    assert LLMExceptionResolver(StaticChatClient("{}")).model == "claude-opus-4-8"


# --------------------------------------------------------------------------
# Safe degradation — every bad output becomes ResolverError
# --------------------------------------------------------------------------
def test_non_json_raises_resolver_error():
    with pytest.raises(ResolverError):
        _assess("I think these match, probably.")


def test_json_not_object_raises():
    with pytest.raises(ResolverError):
        _assess("[1, 2, 3]")


def test_missing_field_raises():
    with pytest.raises(ResolverError):
        _assess(json.dumps({"is_match": True, "confidence": 0.9}))  # no rationale


def test_is_match_not_bool_raises():
    with pytest.raises(ResolverError):
        _assess(json.dumps({"is_match": "yes", "confidence": 0.9, "rationale": "x"}))


def test_out_of_range_confidence_raises():
    with pytest.raises(ResolverError):
        _assess(_json(True, 1.5))


def test_non_numeric_confidence_raises():
    with pytest.raises(ResolverError):
        _assess(json.dumps({"is_match": True, "confidence": "high", "rationale": "x"}))


def test_client_exception_becomes_resolver_error():
    cand = _candidate()
    with pytest.raises(ResolverError):
        LLMExceptionResolver(RaisingChatClient()).assess(cand.settlement, cand.bank, cand.score)


def test_parse_verdict_rejects_non_text():
    with pytest.raises(ResolverError):
        _parse_verdict(12345)  # type: ignore[arg-type]


def test_sequence_client_exhaustion():
    c = SequenceChatClient([_json(True, 0.9)])
    c.complete("s", "u")
    with pytest.raises(ResolverError):
        c.complete("s", "u")


# --------------------------------------------------------------------------
# Prompt content (sanity)
# --------------------------------------------------------------------------
def test_prompt_includes_both_records():
    captured = {}

    class Capturing:
        def complete(self, system, user):
            captured["system"] = system
            captured["user"] = user
            return _json(True, 0.9)

    cand = _candidate()
    LLMExceptionResolver(Capturing()).assess(cand.settlement, cand.bank, cand.score)
    assert "SETTLEMENT id=s1" in captured["user"]
    assert "BANK CREDIT id=b1" in captured["user"]
    assert "reconciliation adjudicator" in captured["system"]


# --------------------------------------------------------------------------
# Integration with the GatedResolver (the whole point)
# --------------------------------------------------------------------------
def test_gated_resolver_confirms_with_llm_backend():
    base = LLMExceptionResolver(StaticChatClient(_json(True, 0.9)))
    verifier = LLMExceptionResolver(StaticChatClient(_json(True, 0.95)))
    out = GatedResolver(base, verifier).resolve(_candidate())
    assert out.decision is Decision.CONFIRMED_MATCH


def test_gated_resolver_escalates_on_low_confidence_llm():
    base = LLMExceptionResolver(StaticChatClient(_json(True, 0.5)))
    out = GatedResolver(base).resolve(_candidate())
    assert out.decision is Decision.ESCALATE_HUMAN


def test_gated_resolver_escalates_on_llm_error():
    out = GatedResolver(LLMExceptionResolver(RaisingChatClient())).resolve(_candidate())
    assert out.decision is Decision.ESCALATE_HUMAN
    assert "resolver error" in out.rationale


def test_gated_resolver_rejects_on_negative_llm_consensus():
    base = LLMExceptionResolver(StaticChatClient(_json(False, 0.9)))
    out = GatedResolver(base).resolve(_candidate())
    assert out.decision is Decision.REJECTED


def test_full_pipeline_with_llm_backed_resolver():
    # A garbled-UTR pair flows exact -> probabilistic review -> LLM-gated resolver -> confirmed.
    from reclaim.pipeline import run_reclaim
    settlements = [Transaction(id="s1", source=Source.SETTLEMENT, gross_amount=Money.of("2000.00", "INR"),
                               ts=TS, refs=TransactionRefs(utr="U1"))]
    banks = [Transaction(id="b1", source=Source.BANK, gross_amount=Money.of("2000.00", "INR"),
                         ts=TS, refs=TransactionRefs(utr="U1X"))]
    resolver = GatedResolver(
        LLMExceptionResolver(StaticChatClient(_json(True, 0.9))),
        LLMExceptionResolver(StaticChatClient(_json(True, 0.95))),
    )
    rep = run_reclaim(settlements, banks, resolver=resolver)
    assert rep.ai_confirmed_count == 1
    assert rep.matched_amount == Money.of("2000.00", "INR")
    assert rep.residual_leaks == ()
