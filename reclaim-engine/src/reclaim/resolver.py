"""Gated AI exception resolver — the first layer where an LLM enters.

It consumes the Phase-5 **review band** (ambiguous candidate matches) and
decides whether each is truly the same money. Per the architecture's safety
mandate, the AI is never trusted blindly:

- **Behind an interface.** A model plugs in via the ``ExceptionResolver``
  protocol. Tests use deterministic fakes, so this layer stays fully testable;
  a real LLM implementation is a drop-in later.
- **Gated, not obeyed.** ``GatedResolver`` wraps a base resolver with
  *self-consistency* (sample N times, require a strict majority), a
  *confidence gate*, and an *adversarial verifier* (a second opinion prompted
  to refute). Only a consensus + confident + unrefuted candidate is confirmed.
- **Safe degradation (goal G6).** Anything uncertain — no consensus, low
  confidence, verifier refutation, or *any resolver error* — degrades to
  ``ESCALATE_HUMAN``. The resolver never silently accepts or rejects money on a
  guess, and never moves money itself (confirmation is an input to a later
  gated posting, not an action here).

Every decision carries its samples for the audit trail.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from .probabilistic import ScoredMatch

_ZERO = Decimal("0")
_ONE = Decimal("1")


class ResolverError(Exception):
    """Raised by a resolver implementation to signal it could not produce a result.

    (e.g. an LLM API failure). The gate treats this as 'escalate to human',
    never as a decision.
    """


class Decision(str, Enum):
    CONFIRMED_MATCH = "confirmed_match"
    REJECTED = "rejected"
    ESCALATE_HUMAN = "escalate_human"


@dataclass(frozen=True)
class Assessment:
    """One resolver opinion about a candidate."""

    is_match: bool
    confidence: Decimal
    rationale: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.is_match, bool):
            raise ResolverError("is_match must be bool")
        if not isinstance(self.confidence, Decimal) or not (_ZERO <= self.confidence <= _ONE):
            raise ResolverError("confidence must be a Decimal in [0,1]")


@runtime_checkable
class ExceptionResolver(Protocol):
    """The seam an LLM (or any model) implements."""

    def assess(self, settlement, bank, prior_score: Decimal) -> Assessment:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class ResolverConfig:
    n_samples: int = 3
    accept_confidence: Decimal = Decimal("0.8")

    def __post_init__(self) -> None:
        if not isinstance(self.n_samples, int) or isinstance(self.n_samples, bool) or self.n_samples < 1:
            raise ResolverError("n_samples must be a positive int")
        if not isinstance(self.accept_confidence, Decimal) or not (_ZERO <= self.accept_confidence <= _ONE):
            raise ResolverError("accept_confidence must be a Decimal in [0,1]")


DEFAULT_CONFIG = ResolverConfig()


@dataclass(frozen=True)
class ResolutionOutcome:
    candidate: ScoredMatch
    decision: Decision
    confidence: Decimal
    rationale: str
    samples: tuple[Assessment, ...] = ()
    refuted: bool = False


def _min_conf(assessments) -> Decimal:
    """Conservative aggregate: the lowest confidence among the given samples."""
    confs = [a.confidence for a in assessments]
    return min(confs) if confs else _ZERO


class GatedResolver:
    """Wraps a base resolver with self-consistency, a confidence gate, and an
    adversarial verifier, degrading safely to human on any uncertainty."""

    def __init__(self, base: ExceptionResolver, verifier: ExceptionResolver | None = None,
                 config: ResolverConfig = DEFAULT_CONFIG) -> None:
        self._base = base
        self._verifier = verifier
        self._config = config

    def resolve(self, candidate: ScoredMatch) -> ResolutionOutcome:
        cfg = self._config
        s, b, prior = candidate.settlement, candidate.bank, candidate.score

        # 1) Self-consistency: sample the base resolver N times.
        samples: list[Assessment] = []
        for _ in range(cfg.n_samples):
            try:
                samples.append(self._base.assess(s, b, prior))
            except ResolverError:
                return ResolutionOutcome(candidate, Decision.ESCALATE_HUMAN, _ZERO,
                                         "base resolver error; degraded to human",
                                         tuple(samples), refuted=False)

        true_votes = [a for a in samples if a.is_match]
        false_votes = [a for a in samples if not a.is_match]
        half = len(samples) / 2

        # 2) Consensus checks.
        if len(false_votes) > half:
            return ResolutionOutcome(candidate, Decision.REJECTED, _min_conf(false_votes),
                                     "consensus: not a match", tuple(samples))
        if len(true_votes) <= half:  # tie / no strict majority
            return ResolutionOutcome(candidate, Decision.ESCALATE_HUMAN, _min_conf(samples),
                                     "no consensus among samples", tuple(samples))

        # 3) Confidence gate (conservative: weakest supporting sample).
        conf = _min_conf(true_votes)
        if conf < cfg.accept_confidence:
            return ResolutionOutcome(candidate, Decision.ESCALATE_HUMAN, conf,
                                     "confidence below accept threshold", tuple(samples))

        # 4) Adversarial verification (a second opinion trying to refute).
        if self._verifier is not None:
            try:
                v = self._verifier.assess(s, b, prior)
            except ResolverError:
                return ResolutionOutcome(candidate, Decision.ESCALATE_HUMAN, conf,
                                         "verifier error; degraded to human",
                                         tuple(samples), refuted=False)
            samples_with_v = tuple(samples) + (v,)
            if not v.is_match:
                return ResolutionOutcome(candidate, Decision.ESCALATE_HUMAN, conf,
                                         "verifier refuted the match", samples_with_v, refuted=True)
            return ResolutionOutcome(candidate, Decision.CONFIRMED_MATCH, conf,
                                     "consensus + confidence + verified", samples_with_v)

        # No verifier: consensus + confidence is sufficient to confirm.
        return ResolutionOutcome(candidate, Decision.CONFIRMED_MATCH, conf,
                                 "consensus + confidence (no verifier)", tuple(samples))

    def resolve_many(self, candidates) -> list[ResolutionOutcome]:
        return [self.resolve(c) for c in candidates]


# --------------------------------------------------------------------------
# Deterministic fake resolvers — for tests and as safe, offline defaults.
# A real LLM-backed resolver implements the same ExceptionResolver protocol.
# --------------------------------------------------------------------------
@dataclass
class StaticResolver:
    """Always returns the same assessment. Deterministic."""

    is_match: bool
    confidence: Decimal
    rationale: str = "static"

    def assess(self, settlement, bank, prior_score: Decimal) -> Assessment:
        return Assessment(self.is_match, self.confidence, self.rationale)


@dataclass
class SequenceResolver:
    """Returns pre-set assessments in order (to simulate sampling disagreement)."""

    assessments: list = field(default_factory=list)
    _i: int = 0

    def assess(self, settlement, bank, prior_score: Decimal) -> Assessment:
        if self._i >= len(self.assessments):
            raise ResolverError("SequenceResolver exhausted")
        a = self.assessments[self._i]
        self._i += 1
        return a


@dataclass
class RaisingResolver:
    """Always raises — models an LLM/API failure to test safe degradation."""

    def assess(self, settlement, bank, prior_score: Decimal) -> Assessment:
        raise ResolverError("simulated resolver failure")
