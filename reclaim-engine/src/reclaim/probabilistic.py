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
# The matcher
# --------------------------------------------------------------------------
def probabilistic_match(
    settlements: list[Transaction],
    banks: list[Transaction],
    config: MatchConfig = DEFAULT_CONFIG,
) -> ProbabilisticResult:
    """Greedy one-to-one fuzzy matching over residual settlements and banks."""
    # Build all candidate pairs at or above the review threshold.
    candidates: list[ScoredMatch] = []
    for s in settlements:
        for b in banks:
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
    )
