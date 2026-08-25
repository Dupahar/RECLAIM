"""Phase 5 tests — probabilistic (fuzzy) matching layer."""
from datetime import datetime
from decimal import Decimal

import pytest

from reclaim.money import Money
from reclaim.domain import Source, Transaction, TransactionRefs
from reclaim.probabilistic import (
    DEFAULT_CONFIG,
    MatchConfig,
    MatchWeights,
    ProbabilisticError,
    amount_score,
    date_score,
    probabilistic_match,
    reference_score,
    score_pair,
)

D = Decimal


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def s(sid, amount, day=25, utr=None, order_id=None):
    return Transaction(id=sid, source=Source.SETTLEMENT, gross_amount=inr(amount),
                       ts=datetime(2026, 8, day, 12, 0, 0),
                       refs=TransactionRefs(utr=utr, order_id=order_id))


def b(bid, amount, day=25, utr=None, order_id=None):
    return Transaction(id=bid, source=Source.BANK, gross_amount=inr(amount),
                       ts=datetime(2026, 8, day, 12, 0, 0),
                       refs=TransactionRefs(utr=utr, order_id=order_id))


# --------------------------------------------------------------------------
# Config validation
# --------------------------------------------------------------------------
def test_weights_must_sum_to_one():
    with pytest.raises(ProbabilisticError):
        MatchWeights(amount=D("0.5"), date=D("0.2"), reference=D("0.1"))


def test_weights_non_negative_decimal():
    with pytest.raises(ProbabilisticError):
        MatchWeights(amount=D("-0.1"), date=D("0.6"), reference=D("0.5"))


def test_config_threshold_ordering():
    with pytest.raises(ProbabilisticError):
        MatchConfig(match_threshold=D("0.5"), review_threshold=D("0.8"))


def test_config_tolerance_and_maxdays():
    with pytest.raises(ProbabilisticError):
        MatchConfig(amount_tolerance=D("0"))
    with pytest.raises(ProbabilisticError):
        MatchConfig(max_days=-1)
    with pytest.raises(ProbabilisticError):
        MatchConfig(max_days=True)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Field scorers
# --------------------------------------------------------------------------
def test_amount_score_exact_and_currency_guard():
    assert amount_score(inr("100.00"), inr("100.00"), D("0.01")) == D("1")
    assert amount_score(inr("100.00"), Money.of("100.00", "USD"), D("0.01")) == D("0")


def test_amount_score_zero_expected():
    assert amount_score(Money.zero("INR"), Money.zero("INR"), D("0.01")) == D("1")
    assert amount_score(Money.zero("INR"), inr("1.00"), D("0.01")) == D("0")


def test_amount_score_within_and_beyond_tolerance():
    # 0.5% diff with 1% tolerance -> 1 - 0.5 = 0.5
    assert amount_score(inr("1000.00"), inr("1005.00"), D("0.01")) == D("0.5000")
    # 2% diff with 1% tolerance -> clamped to 0
    assert amount_score(inr("1000.00"), inr("1020.00"), D("0.01")) == D("0")


def test_date_score():
    d0 = datetime(2026, 8, 25, 0, 0)
    assert date_score(d0, datetime(2026, 8, 25, 23, 0), 3) == D("1")   # same day
    assert date_score(d0, datetime(2026, 8, 26, 0, 0), 3) == D("0.6667")  # 1 of 3 days
    assert date_score(d0, datetime(2026, 8, 30, 0, 0), 3) == D("0")    # beyond
    # max_days == 0 -> strict same-day
    assert date_score(d0, d0, 0) == D("1")
    assert date_score(d0, datetime(2026, 8, 26), 0) == D("0")


def test_reference_score():
    assert reference_score(TransactionRefs(utr="U1"), TransactionRefs(utr="U1")) == D("1")
    assert reference_score(TransactionRefs(order_id="O9"), TransactionRefs(order_id="O9")) == D("1")
    assert reference_score(TransactionRefs(utr="U1"), TransactionRefs(utr="U2")) == D("0")
    assert reference_score(TransactionRefs(), TransactionRefs()) == D("0")  # both empty


def test_score_pair_combinations():
    # exact amount + same day + shared order_id -> 0.5 + 0.2 + 0.3 = 1.0
    assert score_pair(s("s1", "100.00", order_id="O1"), b("b1", "100.00", order_id="O1")) == D("1.0000")
    # exact amount + same day + no shared ref -> 0.7
    assert score_pair(s("s1", "100.00"), b("b1", "100.00")) == D("0.7000")


# --------------------------------------------------------------------------
# The matcher: bands, one-to-one, residual, determinism
# --------------------------------------------------------------------------
def test_auto_match_band():
    res = probabilistic_match([s("s1", "100.00", order_id="O1")], [b("b1", "100.00", order_id="O1")])
    assert res.auto_count == 1 and res.review_count == 0
    assert res.auto_matches[0].score == D("1.0000")
    assert res.auto_matches[0].band == "auto"


def test_review_band():
    # exact amount + same day + no shared ref -> 0.70 -> review (0.6..0.9)
    res = probabilistic_match([s("s1", "100.00")], [b("b1", "100.00")])
    assert res.review_count == 1 and res.auto_count == 0
    assert res.review_candidates[0].score == D("0.7000")


def test_residual_when_below_review():
    # different amount (beyond tol) + far date + no ref -> low -> residual
    res = probabilistic_match([s("s1", "100.00", day=25)], [b("b1", "500.00", day=30)])
    assert res.auto_count == 0 and res.review_count == 0
    assert len(res.residual_settlements) == 1 and len(res.residual_banks) == 1


def test_one_to_one_greedy_assignment():
    # two settlements both perfectly match the single bank; only one wins.
    settlements = [s("s1", "100.00", order_id="O1"), s("s2", "100.00", order_id="O1")]
    banks = [b("b1", "100.00", order_id="O1")]
    res = probabilistic_match(settlements, banks)
    assert res.auto_count == 1
    assert len(res.residual_settlements) == 1
    assert len(res.residual_banks) == 0
    # deterministic tie-break: s1 (lower id) wins
    assert res.auto_matches[0].settlement.id == "s1"


def test_determinism():
    settlements = [s("s1", "100.00"), s("s2", "200.00")]
    banks = [b("b1", "100.00"), b("b2", "200.00")]
    assert probabilistic_match(settlements, banks) == probabilistic_match(settlements, banks)


def test_empty_inputs():
    res = probabilistic_match([], [])
    assert res.auto_count == 0 and res.review_count == 0
    assert res.residual_settlements == () and res.residual_banks == ()


def test_default_config_is_shared_and_valid():
    assert isinstance(DEFAULT_CONFIG, MatchConfig)
    assert DEFAULT_CONFIG.weights.amount + DEFAULT_CONFIG.weights.date + DEFAULT_CONFIG.weights.reference == D("1")
