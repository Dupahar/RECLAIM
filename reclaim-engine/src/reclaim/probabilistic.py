"""Probabilistic (fuzzy) matching — the "second brain" of reconciliation.

Runs *after* the exact deterministic gate (Phase 4), only on the residual that
had no exact UTR match. It scores each candidate settlement<->bank pair on
weighted field agreement (amount closeness, date proximity, shared reference)
and routes it into one of three bands:

    score >= match_threshold   -> auto-match  (high confidence)
    review_threshold <= score  -> review      (ambiguous; the future AI/human
                                               resolver decides)
    score <  review_threshold  -> residual    (stays a leak)

Design choices (ADR-0002):
- **Transparent & deterministic, not a black box.** A hand-built, auditable
  Fellegi-Sunter-style weighted scorer — no external library, no LLM. Scores
  are ``Decimal`` (never float), field weights are explicit, and greedy
  one-to-one assignment uses a fully deterministic ordering. This keeps the
  layer 100%-testable and explainable, which matters for money. Splink /
  learned models are a scale-up path, not a foundation dependency.
- **It never auto-*acts*.** It proposes matches with a score; acting on a
  review-band candidate is a later, gated decision.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .domain import Transaction, TransactionRefs
from .money import Money

_Q = Decimal("0.0001")  # scores reported to 4 dp
_ZERO = Decimal("0")
_ONE = Decimal("1")


class ProbabilisticError(Exception):
    """Raised on malformed probabilistic-matching configuration or input."""


@dataclass(frozen=True)
class MatchWeights:
    amount: Decimal = Decimal("0.5")
    date: Decimal = Decimal("0.2")
    reference: Decimal = Decimal("0.3")

    def __post_init__(self) -> None:
        for w in (self.amount, self.date, self.reference):
            if not isinstance(w, Decimal) or w < 0:
                raise ProbabilisticError("weights must be non-negative Decimals")
        if (self.amount + self.date + self.reference) != _ONE:
            raise ProbabilisticError("weights must sum to exactly 1")


@dataclass(frozen=True)
class MatchConfig:
    weights: MatchWeights = field(default_factory=MatchWeights)
    match_threshold: Decimal = Decimal("0.9")
    review_threshold: Decimal = Decimal("0.6")
    amount_tolerance: Decimal = Decimal("0.01")  # 1% relative tolerance -> score 0
    max_days: int = 3

    def __post_init__(self) -> None:
        if not (_ZERO <= self.review_threshold <= self.match_threshold <= _ONE):
            raise ProbabilisticError("require 0 <= review_threshold <= match_threshold <= 1")
        if self.amount_tolerance <= 0:
            raise ProbabilisticError("amount_tolerance must be positive")
        if not isinstance(self.max_days, int) or isinstance(self.max_days, bool) or self.max_days < 0:
            raise ProbabilisticError("max_days must be a non-negative int")


DEFAULT_CONFIG = MatchConfig()


@dataclass(frozen=True)
class ScoredMatch:
    settlement: Transaction
    bank: Transaction
    score: Decimal
    band: str  # "auto" | "review"


@dataclass(frozen=True)
class ProbabilisticResult:
    auto_matches: tuple[ScoredMatch, ...]
    review_candidates: tuple[ScoredMatch, ...]
    residual_settlements: tuple[Transaction, ...]
    residual_banks: tuple[Transaction, ...]
    pairs_compared: int = 0      # how many pairs blocking actually scored
    pairs_total: int = 0         # len(settlements) * len(banks) -- the naive cost

    @property
    def auto_count(self) -> int:
        return len(self.auto_matches)

    @property
    def review_count(self) -> int:
        return len(self.review_candidates)


# --------------------------------------------------------------------------
# Field scorers — each returns a Decimal in [0, 1]
# --------------------------------------------------------------------------
def amount_score(expected: Money, actual: Money, tolerance: Decimal) -> Decimal:
    """1.0 for an exact amount; decays to 0 at ``tolerance`` relative difference."""
    if expected.currency != actual.currency:
        return _ZERO
    if expected.is_zero:
        return _ONE if actual.is_zero else _ZERO
    # abs() in the denominator keeps rel >= 0 for any sign of expected, so the
    # score can never exceed 1 (no unreachable upper clamp needed).
    rel = abs((expected - actual).amount) / abs(expected.amount)
    score = _ONE - (rel / tolerance)
    if score < _ZERO:
        return _ZERO
    return score.quantize(_Q)


def date_score(d1: datetime, d2: datetime, max_days: int) -> Decimal:
    """1.0 for the same day; linear decay to 0 at ``max_days`` apart."""
    days = abs((d1.date() - d2.date()).days)
    if max_days == 0:
        return _ONE if days == 0 else _ZERO
    if days > max_days:
        return _ZERO
    return (_ONE - Decimal(days) / Decimal(max_days)).quantize(_Q)


def reference_score(r1: TransactionRefs, r2: TransactionRefs) -> Decimal:
    """1.0 if any non-empty reference field matches exactly, else 0.0."""
    for attr in ("utr", "rrn", "order_id", "invoice_no"):
        v1 = getattr(r1, attr)
        v2 = getattr(r2, attr)
        if v1 and v2 and v1 == v2:
            return _ONE
    return _ZERO


def score_pair(settlement: Transaction, bank: Transaction, config: MatchConfig = DEFAULT_CONFIG) -> Decimal:
    """Weighted total score in [0,1] for a settlement<->bank candidate pair."""
    w = config.weights
    a = amount_score(settlement.net_amount, bank.gross_amount, config.amount_tolerance)
    d = date_score(settlement.ts, bank.ts, config.max_days)
    r = reference_score(settlement.refs, bank.refs)
    return (w.amount * a + w.date * d + w.reference * r).quantize(_Q)


# --------------------------------------------------------------------------
# Blocking / candidate generation (architecture Layer 3, stage 1)
#
# Comparing every settlement against every bank credit is O(N*M) -- 10k x 10k is
# 100M Decimal scorings, which is the practical ceiling on batch size. The
# standard entity-resolution fix is *blocking*: only compare records that share
# a blocking key.
#
# Blocking normally trades recall for speed. Here it does not, because the block
# is **derived from the scoring config rather than guessed**, so it can be shown
# to discard only pairs that could not have cleared ``review_threshold`` anyway:
#
#     score = w_a*a + w_d*d + w_r*r,  with a, d, r in [0, 1]
#
# Even if date and reference agree perfectly, the pair still needs
#
#     w_a*a >= review_threshold - w_d - w_r
#
# so any pair whose *amount* score falls below
#
#     a_min = (review_threshold - w_d - w_r) / w_a
#
# is unreachable regardless of every other field. With the default weights
# (0.5 / 0.2 / 0.3) and threshold 0.6 that gives a_min = 0.2, i.e. amounts must
# agree to within 0.8% -- a narrow, cheap window to search.
#
# When a_min <= 0 (a weighting where amount agreement is not required at all),
# no sound amount block exists and we fall back to comparing every pair. The
# optimisation is silently skipped rather than silently lossy.
# --------------------------------------------------------------------------
def min_amount_score(config: MatchConfig = DEFAULT_CONFIG) -> Decimal:
    """Lowest amount score a pair could have and still reach ``review_threshold``.

    Returns 0 when amount agreement is not required (no sound block exists).
    """
    w = config.weights
    if w.amount == _ZERO:
        return _ZERO
    needed = config.review_threshold - w.date - w.reference
    if needed <= _ZERO:
        return _ZERO
    return needed / w.amount


def _max_relative_difference(config: MatchConfig) -> Decimal:
    """Widest relative amount gap that can still clear the review threshold.

    ``amount_score`` is ``1 - rel/tolerance``, so ``score >= a_min`` means
    ``rel <= tolerance * (1 - a_min)``. The window is then widened by one score
    quantum: ``amount_score`` rounds to 4 dp, and a pair sitting a hair below the
    bound can round *up* onto it. Widening keeps the block a strict superset of
    what scoring would accept -- too wide merely costs a comparison, too narrow
    would lose a real match.
    """
    a_min = min_amount_score(config)
    return config.amount_tolerance * (_ONE - a_min + _Q)


def candidate_pairs(settlements, banks, config: MatchConfig = DEFAULT_CONFIG):
    """Yield the (settlement, bank) pairs worth scoring.

    Provably a superset of the pairs that could clear ``review_threshold``; see
    the module note above. Yields in a deterministic order.
    """
    if min_amount_score(config) <= _ZERO:
        for s in settlements:                      # no sound block -- compare everything
            for b in banks:
                yield s, b
        return

    max_rel = _max_relative_difference(config)

    # A currency mismatch scores 0 on amount, so blocks never span currencies.
    by_currency: dict[str, list[Transaction]] = {}
    for b in banks:
        by_currency.setdefault(b.gross_amount.currency, []).append(b)
    for bucket in by_currency.values():
        bucket.sort(key=lambda b: (b.gross_amount.amount, b.id))
    sorted_amounts = {cur: [b.gross_amount.amount for b in bucket]
                      for cur, bucket in by_currency.items()}

    for s in settlements:
        expected = s.net_amount
        bucket = by_currency.get(expected.currency)
        if not bucket:
            continue
        if expected.is_zero:
            # amount_score is 1 for a zero-vs-zero pair and 0 otherwise, so the
            # only reachable partners are the exactly-zero credits.
            lo = hi = Decimal("0")
        else:
            delta = abs(expected.amount) * max_rel
            lo, hi = expected.amount - delta, expected.amount + delta
        amounts = sorted_amounts[expected.currency]
        start = bisect.bisect_left(amounts, lo)
        stop = bisect.bisect_right(amounts, hi)
        for b in bucket[start:stop]:
            yield s, b


# --------------------------------------------------------------------------
# The matcher
# --------------------------------------------------------------------------
def probabilistic_match(
    settlements: list[Transaction],
    banks: list[Transaction],
    config: MatchConfig = DEFAULT_CONFIG,
) -> ProbabilisticResult:
    """Greedy one-to-one fuzzy matching over residual settlements and banks.

    Only *blocked* candidate pairs are scored (see ``candidate_pairs``); the
    result is identical to comparing every pair, at a fraction of the cost.
    """
    candidates: list[ScoredMatch] = []
    compared = 0
    for s, b in candidate_pairs(settlements, banks, config):
        compared += 1
        sc = score_pair(s, b, config)
        if sc >= config.review_threshold:
            band = "auto" if sc >= config.match_threshold else "review"
            candidates.append(ScoredMatch(settlement=s, bank=b, score=sc, band=band))

    # Deterministic ordering: highest score first, ties broken by (s.id, b.id).
    candidates.sort(key=lambda m: (m.settlement.id, m.bank.id))          # stable secondary
    candidates.sort(key=lambda m: m.score, reverse=True)                 # primary

    used_settlements: set[str] = set()
    used_banks: set[str] = set()
    auto: list[ScoredMatch] = []
    review: list[ScoredMatch] = []
    for m in candidates:
        if m.settlement.id in used_settlements or m.bank.id in used_banks:
            continue
        used_settlements.add(m.settlement.id)
        used_banks.add(m.bank.id)
        (auto if m.band == "auto" else review).append(m)

    residual_settlements = tuple(s for s in settlements if s.id not in used_settlements)
    residual_banks = tuple(b for b in banks if b.id not in used_banks)
    return ProbabilisticResult(
        auto_matches=tuple(auto),
        review_candidates=tuple(review),
        residual_settlements=residual_settlements,
        residual_banks=residual_banks,
        pairs_compared=compared,
        pairs_total=len(settlements) * len(banks),
    )
