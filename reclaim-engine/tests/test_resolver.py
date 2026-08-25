"""Phase 6 tests — gated AI exception resolver.

Uses deterministic fake resolvers so the whole gating pipeline is provable
without any real (stochastic) model.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from reclaim.money import Money
from reclaim.domain import Source, Transaction, TransactionRefs
from reclaim.probabilistic import ScoredMatch
from reclaim.resolver import (
    Assessment,
    Decision,
    GatedResolver,
    RaisingResolver,
    ResolverConfig,
    ResolverError,
    SequenceResolver,
    StaticResolver,
)

D = Decimal
TS = datetime(2026, 8, 25, 9, 0, 0)


def _candidate(score="0.7000") -> ScoredMatch:
    s = Transaction(id="s1", source=Source.SETTLEMENT, gross_amount=Money.of("100.00", "INR"),
                    ts=TS, refs=TransactionRefs(utr="U1"))
    b = Transaction(id="b1", source=Source.BANK, gross_amount=Money.of("100.00", "INR"),
                    ts=TS, refs=TransactionRefs(utr="U1X"))
    return ScoredMatch(settlement=s, bank=b, score=D(score), band="review")


# --------------------------------------------------------------------------
# Assessment / config validation
# --------------------------------------------------------------------------
def test_assessment_validates():
    with pytest.raises(ResolverError):
        Assessment(is_match="yes", confidence=D("0.9"))  # type: ignore[arg-type]
    with pytest.raises(ResolverError):
        Assessment(is_match=True, confidence=D("1.5"))
    with pytest.raises(ResolverError):
        Assessment(is_match=True, confidence=0.9)  # type: ignore[arg-type]  # float rejected


def test_config_validates():
    with pytest.raises(ResolverError):
        ResolverConfig(n_samples=0)
    with pytest.raises(ResolverError):
        ResolverConfig(n_samples=True)  # type: ignore[arg-type]
    with pytest.raises(ResolverError):
        ResolverConfig(accept_confidence=D("2"))


# --------------------------------------------------------------------------
# Happy path — consensus + confidence + verifier
# --------------------------------------------------------------------------
def test_confirmed_match_with_verifier():
    base = StaticResolver(is_match=True, confidence=D("0.9"))
    verifier = StaticResolver(is_match=True, confidence=D("0.95"))
    gr = GatedResolver(base, verifier)
    out = gr.resolve(_candidate())
    assert out.decision is Decision.CONFIRMED_MATCH
    assert out.confidence == D("0.9")
    assert len(out.samples) == 4  # 3 base + 1 verifier
    assert out.refuted is False


def test_confirmed_match_without_verifier():
    gr = GatedResolver(StaticResolver(is_match=True, confidence=D("0.85")))
    out = gr.resolve(_candidate())
    assert out.decision is Decision.CONFIRMED_MATCH
    assert len(out.samples) == 3


# --------------------------------------------------------------------------
# Safe degradation paths — all end in ESCALATE_HUMAN or REJECTED, never a guess
# --------------------------------------------------------------------------
def test_rejected_on_negative_consensus():
    gr = GatedResolver(StaticResolver(is_match=False, confidence=D("0.9")))
    out = gr.resolve(_candidate())
    assert out.decision is Decision.REJECTED


def test_no_consensus_escalates():
    # 3 samples split 2 True / 1 False is a majority True; to get *no* strict
    # majority use an even n_samples split 1-1... use n_samples=2 with a tie.
    base = SequenceResolver([Assessment(True, D("0.9")), Assessment(False, D("0.9"))])
    gr = GatedResolver(base, config=ResolverConfig(n_samples=2))
    out = gr.resolve(_candidate())
    assert out.decision is Decision.ESCALATE_HUMAN
    assert "consensus" in out.rationale


def test_low_confidence_escalates():
    gr = GatedResolver(StaticResolver(is_match=True, confidence=D("0.5")))
    out = gr.resolve(_candidate())
    assert out.decision is Decision.ESCALATE_HUMAN
    assert out.confidence == D("0.5")


def test_verifier_refutation_escalates():
    base = StaticResolver(is_match=True, confidence=D("0.9"))
    verifier = StaticResolver(is_match=False, confidence=D("0.9"))  # refutes
    out = GatedResolver(base, verifier).resolve(_candidate())
    assert out.decision is Decision.ESCALATE_HUMAN
    assert out.refuted is True


def test_base_resolver_error_degrades_to_human():
    out = GatedResolver(RaisingResolver()).resolve(_candidate())
    assert out.decision is Decision.ESCALATE_HUMAN
    assert "resolver error" in out.rationale


def test_verifier_error_degrades_to_human():
    base = StaticResolver(is_match=True, confidence=D("0.9"))
    out = GatedResolver(base, RaisingResolver()).resolve(_candidate())
    assert out.decision is Decision.ESCALATE_HUMAN
    assert "verifier error" in out.rationale


# --------------------------------------------------------------------------
# Majority handling with odd n
# --------------------------------------------------------------------------
def test_majority_true_confirms():
    # 2 True / 1 False, all high confidence -> majority True -> confirmed.
    base = SequenceResolver([
        Assessment(True, D("0.9")), Assessment(False, D("0.9")), Assessment(True, D("0.9")),
    ])
    gr = GatedResolver(base, config=ResolverConfig(n_samples=3))
    out = gr.resolve(_candidate())
    assert out.decision is Decision.CONFIRMED_MATCH
    # conservative confidence = min over TRUE votes = 0.9
    assert out.confidence == D("0.9")


def test_majority_false_rejects():
    base = SequenceResolver([
        Assessment(False, D("0.9")), Assessment(True, D("0.9")), Assessment(False, D("0.9")),
    ])
    out = GatedResolver(base, config=ResolverConfig(n_samples=3)).resolve(_candidate())
    assert out.decision is Decision.REJECTED


# --------------------------------------------------------------------------
# Batch + determinism
# --------------------------------------------------------------------------
def test_sequence_resolver_exhaustion_raises():
    seq = SequenceResolver([Assessment(True, D("0.9"))])
    seq.assess(None, None, D("0"))          # consumes the only item
    with pytest.raises(ResolverError):
        seq.assess(None, None, D("0"))      # exhausted -> raises


def test_resolve_many_and_determinism():
    gr = GatedResolver(StaticResolver(is_match=True, confidence=D("0.9")),
                       StaticResolver(is_match=True, confidence=D("0.9")))
    cands = [_candidate(), _candidate("0.65")]
    a = gr.resolve_many(cands)
    b = gr.resolve_many(cands)
    assert [o.decision for o in a] == [o.decision for o in b] == [Decision.CONFIRMED_MATCH] * 2
