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


# --------------------------------------------------------------------------
# Blocking (Phase 18) — a candidate-generation step must never change the
# answer, only the cost of reaching it. These tests are the proof.
# --------------------------------------------------------------------------
import random

from reclaim.probabilistic import (
    ProbabilisticResult,
    candidate_pairs,
    min_amount_score,
    score_pair as _score_pair,
)


def _brute_force_match(settlements, banks, config=DEFAULT_CONFIG):
    """Independent oracle: the pre-blocking algorithm, comparing every pair."""
    candidates = []
    for st in settlements:
        for bk in banks:
            sc = _score_pair(st, bk, config)
            if sc >= config.review_threshold:
                band = "auto" if sc >= config.match_threshold else "review"
                candidates.append((sc, st, bk, band))
    candidates.sort(key=lambda m: (m[1].id, m[2].id))
    candidates.sort(key=lambda m: m[0], reverse=True)
    used_s, used_b, auto, review = set(), set(), [], []
    for sc, st, bk, band in candidates:
        if st.id in used_s or bk.id in used_b:
            continue
        used_s.add(st.id)
        used_b.add(bk.id)
        (auto if band == "auto" else review).append((st.id, bk.id, sc))
    return auto, review


def _as_pairs(result: ProbabilisticResult):
    return ([(m.settlement.id, m.bank.id, m.score) for m in result.auto_matches],
            [(m.settlement.id, m.bank.id, m.score) for m in result.review_candidates])


def test_min_amount_score_derivation():
    # default weights 0.5/0.2/0.3, review threshold 0.6 -> (0.6-0.2-0.3)/0.5 = 0.2
    assert min_amount_score(DEFAULT_CONFIG) == D("0.2")


def test_no_sound_block_when_amount_not_required():
    """If date+reference alone can clear the threshold, blocking must be skipped."""
    cfg = MatchConfig(weights=MatchWeights(amount=D("0.2"), date=D("0.3"), reference=D("0.5")),
                      review_threshold=D("0.6"))
    assert min_amount_score(cfg) == D("0")
    sts = [s("s1", "100.00"), s("s2", "200.00")]
    bks = [b("b1", "999.00"), b("b2", "888.00")]
    # every pair is still generated -- no silent recall loss
    assert len(list(candidate_pairs(sts, bks, cfg))) == 4


def test_blocking_skips_amount_mismatches():
    sts = [s("s1", "1000.00")]
    bks = [b("bnear", "1000.00"), b("bfar", "5000.00"), b("bfar2", "77.00")]
    pairs = list(candidate_pairs(sts, bks, DEFAULT_CONFIG))
    assert [bk.id for _, bk in pairs] == ["bnear"]


def test_blocking_never_spans_currencies():
    usd = Transaction(id="busd", source=Source.BANK, gross_amount=Money.of("1000.00", "USD"),
                      ts=datetime(2026, 8, 25, 12, 0, 0), refs=TransactionRefs())
    pairs = list(candidate_pairs([s("s1", "1000.00")], [usd], DEFAULT_CONFIG))
    assert pairs == []


def test_blocking_handles_zero_expected_amount():
    zero_s = Transaction(id="s0", source=Source.SETTLEMENT, gross_amount=inr("0"),
                         ts=datetime(2026, 8, 25, 12, 0, 0), refs=TransactionRefs())
    pairs = list(candidate_pairs([zero_s], [b("bz", "0"), b("bnz", "10.00")], DEFAULT_CONFIG))
    assert [bk.id for _, bk in pairs] == ["bz"]


def test_blocking_reports_its_own_saving():
    sts = [s(f"s{i}", f"{1000 + i * 50}.00") for i in range(20)]
    bks = [b(f"b{i}", f"{1000 + i * 50}.00") for i in range(20)]
    res = probabilistic_match(sts, bks)
    assert res.pairs_total == 400
    assert res.pairs_compared == 20          # one real partner each, not 400
    # amount + date agree but no shared reference -> 0.7, the review band
    assert len(res.review_candidates) == 20
    assert res.residual_settlements == () and res.residual_banks == ()


def test_blocking_is_lossless_against_brute_force():
    """The property that makes blocking safe for money: identical results.

    A seeded corpus of near-miss amounts, shifted dates and shared references —
    exactly the shapes where a careless block would drop a real match.
    """
    rnd = random.Random(20260828)
    for case in range(60):
        sts, bks = [], []
        for i in range(12):
            base = rnd.choice(["500.00", "1000.00", "1500.50", "99.99", "20000.00"])
            sts.append(s(f"s{case}_{i}", base,
                         day=rnd.choice([24, 25, 26]),
                         utr=rnd.choice([None, f"U{i}"]),
                         order_id=rnd.choice([None, f"O{i}"])))
            # deliberately generate near-misses straddling the tolerance edge
            drift = rnd.choice(["1", "0.999", "1.001", "0.995", "1.02", "0.8"])
            amt = (Decimal(base) * Decimal(drift)).quantize(Decimal("0.01"))
            bks.append(b(f"b{case}_{i}", str(amt),
                         day=rnd.choice([24, 25, 26]),
                         utr=rnd.choice([None, f"U{i}"]),
                         order_id=rnd.choice([None, f"O{i}"])))
        expected_auto, expected_review = _brute_force_match(sts, bks)
        got_auto, got_review = _as_pairs(probabilistic_match(sts, bks))
        assert got_auto == expected_auto, f"case {case}: auto-match set changed"
        assert got_review == expected_review, f"case {case}: review set changed"


def test_blocking_lossless_under_alternative_weights():
    """The block is derived from config, so it must hold for other weightings."""
    cfg = MatchConfig(weights=MatchWeights(amount=D("0.7"), date=D("0.1"), reference=D("0.2")),
                      review_threshold=D("0.5"), match_threshold=D("0.85"),
                      amount_tolerance=D("0.05"), max_days=5)
    rnd = random.Random(7)
    for case in range(25):
        sts = [s(f"s{case}_{i}", str(rnd.randrange(100, 5000)) + ".00",
                 day=rnd.choice([23, 25, 27])) for i in range(10)]
        bks = [b(f"b{case}_{i}", str(rnd.randrange(100, 5000)) + ".00",
                 day=rnd.choice([23, 25, 27])) for i in range(10)]
        exp = _brute_force_match(sts, bks, cfg)
        got = _as_pairs(probabilistic_match(sts, bks, cfg))
        assert list(got) == list(exp), f"case {case}"


def test_min_amount_score_zero_when_amount_weight_is_zero():
    """Amount carries no weight -> no amount block is sound, so none is applied."""
    cfg = MatchConfig(weights=MatchWeights(amount=D("0"), date=D("0.4"), reference=D("0.6")))
    assert min_amount_score(cfg) == D("0")
    sts, bks = [s("s1", "1.00")], [b("b1", "999999.00")]
    assert len(list(candidate_pairs(sts, bks, cfg))) == 1     # compared despite the amount gap
