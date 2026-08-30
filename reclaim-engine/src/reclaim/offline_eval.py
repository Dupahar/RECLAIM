"""Offline policy evaluation — grade a policy before a customer feels it.

Architecture §7: *"...with **offline policy evaluation via Inverse Propensity
Scoring / Doubly Robust** so we can validate a new policy **before** deploying
it."* Also §9.2, which asks for the same machinery as a drift monitor.

This is the safety valve on the whole learning layer. A bandit that has learned
something plausible is not evidence that acting on it is an improvement, and the
only ethical way to find out — for a system that debits people's accounts — is to
ask the question of *logged* data first.

    IPS:  E[r under target] ~ mean( (pi_target(a|x) / pi_logged(a|x)) * r )
    DR:   the same, with a reward model absorbing most of the variance

**What this module refuses to do, which is most of its value.** An importance-
weighted estimator will always return a number. Whether that number means
anything depends on conditions the arithmetic cannot see, so each is checked and
reported rather than assumed:

- **Zero propensity raises.** A logged action the policy had no chance of taking
  puts a zero in the denominator. There is no defensible way to continue.
- **No overlap means not identified.** If the target policy wants an action that
  never appears in the log for that context, no amount of reweighting can say
  what it would have achieved. The estimate is `None` with the unsupported
  contexts named — not a number with a caveat attached.
- **Effective sample size is always reported.** An IPS estimate where one row
  carries 90% of the weight is a sample of one wearing a sample of a thousand's
  clothing. `ess` and `max_weight_share` make that visible; `concentrated` says
  so in words.
- **Clipping is counted and its direction stated.** Capping weights reduces
  variance and biases the estimate *toward the logging policy*, which means it
  understates a genuinely better target. A caller who clips should know which way
  they have leaned.

**Why DR is worth the extra moving part.** Doubly robust is unbiased if *either*
the reward model or the propensities are right. The test that earns its keep here
gives it a perfect reward model and deliberately corrupted propensities, and it
still recovers the true value — which is exactly the situation a real deployment
is in, since nobody's propensities survive a production incident intact.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Iterable, Optional, Sequence

_Q = Decimal("0.0001")
_ZERO = Decimal("0")
_ONE = Decimal("1")

# Below this share of n, an estimate rests on too few effectively-independent
# rows to be worth acting on. Reported, not enforced -- the caller decides.
_ESS_FLOOR = Decimal("0.10")
# Above this, a single row dominates the estimate.
_DOMINANCE = Decimal("0.50")


class OfflineEvalError(Exception):
    """Raised when an offline estimate cannot be computed honestly."""


@dataclass(frozen=True)
class LoggedDecision:
    """One decision the deployed policy actually made, with its outcome.

    ``propensity`` must be the probability recorded *at decision time*. A
    propensity recomputed later, after the policy has been updated, is a
    different number from the one that generated the data, and substituting it
    biases every estimate in a direction nobody can see.
    """

    unit_id: str
    context_key: str
    action_key: str
    propensity: Decimal
    reward: Decimal

    def __post_init__(self) -> None:
        for name, val in (("unit_id", self.unit_id), ("context_key", self.context_key),
                          ("action_key", self.action_key)):
            if not (isinstance(val, str) and val):
                raise OfflineEvalError(f"{name} must be a non-empty string")
        if not isinstance(self.propensity, Decimal):
            raise OfflineEvalError("propensity must be a Decimal")
        if not isinstance(self.reward, Decimal):
            raise OfflineEvalError("reward must be a Decimal")
        if self.propensity <= _ZERO:
            raise OfflineEvalError(
                f"propensity for {self.unit_id!r} is {self.propensity}; a logged action "
                "the policy could not have taken puts a zero in the denominator and "
                "there is no defensible way to continue")
        if self.propensity > _ONE:
            raise OfflineEvalError(
                f"propensity for {self.unit_id!r} is {self.propensity}, above 1")


# A target policy only has to answer "what probability would you give this
# action in this context?". Anything with that shape works, which keeps the
# evaluator independent of how the candidate policy is implemented.
Policy = Callable[[str, str], Decimal]
# A reward model estimates the outcome of an untaken action.
RewardModel = Callable[[str, str], Decimal]


@dataclass(frozen=True)
class Estimate:
    """An offline estimate and every reason to distrust it."""

    method: str
    value: Optional[Decimal]
    n: int
    logged_mean: Decimal
    ess: Optional[Decimal]
    max_weight_share: Optional[Decimal]
    clipped: int
    identified: bool
    unsupported: tuple[str, ...]
    note: str

    @property
    def is_usable(self) -> bool:
        """Identified, and not resting on a handful of dominant rows."""
        return self.identified and not self.concentrated

    @property
    def concentrated(self) -> bool:
        if self.ess is None or self.max_weight_share is None:
            return False
        return (self.ess < _ESS_FLOOR * Decimal(self.n)
                or self.max_weight_share > _DOMINANCE)

    def improvement_over_logged(self) -> Optional[Decimal]:
        """How much better the target looks than what was actually deployed."""
        if self.value is None:
            return None
        return (self.value - self.logged_mean).quantize(_Q)

    def summary(self) -> dict:
        def s(v):
            return None if v is None else str(v)
        return {"method": self.method, "value": s(self.value), "n": self.n,
                "logged_mean": str(self.logged_mean), "ess": s(self.ess),
                "max_weight_share": s(self.max_weight_share), "clipped": self.clipped,
                "identified": self.identified, "concentrated": self.concentrated,
                "usable": self.is_usable, "unsupported": list(self.unsupported),
                "improvement": s(self.improvement_over_logged()), "note": self.note}


def _prepare(log: Iterable[LoggedDecision]) -> list[LoggedDecision]:
    rows = list(log)
    if not rows:
        raise OfflineEvalError("cannot evaluate a policy against an empty log")
    for row in rows:
        if not isinstance(row, LoggedDecision):
            raise OfflineEvalError("every log row must be a LoggedDecision")
    return rows


def _logged_mean(rows: Sequence[LoggedDecision]) -> Decimal:
    total = sum((r.reward for r in rows), _ZERO)
    return (total / Decimal(len(rows))).quantize(_Q)


def _support(rows: Sequence[LoggedDecision]) -> dict:
    """Which actions were actually observed in each context."""
    seen: dict[str, set] = {}
    for row in rows:
        seen.setdefault(row.context_key, set()).add(row.action_key)
    return seen


def _unsupported_contexts(rows, policy: Policy, actions: Sequence[str]) -> tuple[str, ...]:
    """Contexts where the target wants an action the log never contains.

    Deterministic order (sorted) so a report is stable across runs.
    """
    seen = _support(rows)
    bad = set()
    for context, observed in seen.items():
        for action in actions:
            if policy(context, action) > _ZERO and action not in observed:
                bad.add(context)
    return tuple(sorted(bad))


def _diagnostics(weights: Sequence[Decimal]) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """Effective sample size and the largest single weight's share.

    ``ess = (sum w)^2 / sum w^2`` — the classic importance-sampling measure of
    how many rows are really contributing.
    """
    total = sum(weights, _ZERO)
    if total == _ZERO:
        return _ZERO, _ZERO
    sq = sum((w * w for w in weights), _ZERO)
    ess = (total * total / sq).quantize(_Q)
    return ess, (max(weights) / total).quantize(_Q)


def _identification_note(unsupported: tuple[str, ...]) -> str:
    return (f"NOT IDENTIFIED: the target policy acts in {len(unsupported)} context/s the "
            f"log never covers ({', '.join(unsupported[:3])}"
            f"{', ...' if len(unsupported) > 3 else ''}); no reweighting can say what "
            "it would have achieved there")


def _concentration_note(ess: Decimal, n: int, share: Decimal) -> str:
    parts = []
    if ess < _ESS_FLOOR * Decimal(n):
        parts.append(f"effective sample size {ess} of {n} rows")
    if share > _DOMINANCE:
        parts.append(f"one row carries {share} of the weight")
    if not parts:
        return ""
    return " | CONCENTRATED: " + "; ".join(parts) + " — treat as indicative, not established"


def ips(log: Iterable[LoggedDecision], policy: Policy, *,
        actions: Sequence[str], clip: Optional[Decimal] = None) -> Estimate:
    """Inverse-propensity estimate of a target policy's mean reward.

    ``actions`` is the full action set, needed to check overlap: without it the
    evaluator can only see the actions that happen to be in the log, which is
    exactly the blind spot the identification check exists to close.
    """
    rows = _prepare(log)
    if clip is not None:
        if not isinstance(clip, Decimal) or clip <= _ZERO:
            raise OfflineEvalError("clip must be a positive Decimal")
    logged = _logged_mean(rows)
    unsupported = _unsupported_contexts(rows, policy, actions)
    if unsupported:
        return Estimate(method="ips", value=None, n=len(rows), logged_mean=logged,
                        ess=None, max_weight_share=None, clipped=0, identified=False,
                        unsupported=unsupported, note=_identification_note(unsupported))

    weights, clipped, total = [], 0, _ZERO
    for row in rows:
        weight = policy(row.context_key, row.action_key) / row.propensity
        if clip is not None and weight > clip:
            weight, clipped = clip, clipped + 1
        weights.append(weight)
        total += weight * row.reward

    ess, share = _diagnostics(weights)
    value = (total / Decimal(len(rows))).quantize(_Q)
    note = f"IPS over {len(rows)} logged decisions"
    if clipped:
        note += (f" | {clipped} weight/s clipped at {clip}: variance down, and the "
                 "estimate is biased *toward the logging policy*, so a genuinely "
                 "better target is understated")
    note += _concentration_note(ess, len(rows), share)
    return Estimate(method="ips", value=value, n=len(rows), logged_mean=logged,
                    ess=ess, max_weight_share=share, clipped=clipped, identified=True,
                    unsupported=(), note=note)


def direct_method(log: Iterable[LoggedDecision], policy: Policy, *,
                  actions: Sequence[str], reward_model: RewardModel) -> Estimate:
    """Model-only estimate: ask the reward model what each policy action pays.

    Ignores the logged rewards entirely, so it has no variance and inherits every
    one of the reward model's mistakes. Reported on its own because seeing it
    next to IPS is how you tell which of the two DR is leaning on.
    """
    rows = _prepare(log)
    logged = _logged_mean(rows)
    total = _ZERO
    for row in rows:
        for action in actions:
            probability = policy(row.context_key, action)
            if probability > _ZERO:
                total += probability * reward_model(row.context_key, action)
    value = (total / Decimal(len(rows))).quantize(_Q)
    return Estimate(method="direct", value=value, n=len(rows), logged_mean=logged,
                    ess=None, max_weight_share=None, clipped=0, identified=True,
                    unsupported=(),
                    note=("direct method: the reward model's opinion only — no logged "
                          "reward enters it, so it is exactly as wrong as the model is"))


def doubly_robust(log: Iterable[LoggedDecision], policy: Policy, *,
                  actions: Sequence[str], reward_model: RewardModel,
                  clip: Optional[Decimal] = None) -> Estimate:
    """Doubly-robust estimate: model prediction plus importance-weighted residual.

    Unbiased if *either* the reward model or the propensities are correct, which
    is the property worth having: in a real deployment neither is reliably true,
    but both being wrong at once is much rarer than one.
    """
    rows = _prepare(log)
    if clip is not None:
        if not isinstance(clip, Decimal) or clip <= _ZERO:
            raise OfflineEvalError("clip must be a positive Decimal")
    logged = _logged_mean(rows)
    unsupported = _unsupported_contexts(rows, policy, actions)
    if unsupported:
        return Estimate(method="dr", value=None, n=len(rows), logged_mean=logged,
                        ess=None, max_weight_share=None, clipped=0, identified=False,
                        unsupported=unsupported, note=_identification_note(unsupported))

    weights, clipped, total = [], 0, _ZERO
    for row in rows:
        baseline = _ZERO
        for action in actions:
            probability = policy(row.context_key, action)
            if probability > _ZERO:
                baseline += probability * reward_model(row.context_key, action)
        weight = policy(row.context_key, row.action_key) / row.propensity
        if clip is not None and weight > clip:
            weight, clipped = clip, clipped + 1
        weights.append(weight)
        residual = row.reward - reward_model(row.context_key, row.action_key)
        total += baseline + weight * residual

    ess, share = _diagnostics(weights)
    value = (total / Decimal(len(rows))).quantize(_Q)
    note = f"doubly robust over {len(rows)} logged decisions"
    if clipped:
        note += (f" | {clipped} weight/s clipped at {clip}: biased toward the logging "
                 "policy")
    note += _concentration_note(ess, len(rows), share)
    return Estimate(method="dr", value=value, n=len(rows), logged_mean=logged,
                    ess=ess, max_weight_share=share, clipped=clipped, identified=True,
                    unsupported=(), note=note)


# --------------------------------------------------------------------------
# The gate a candidate policy has to pass
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Verdict:
    """Ship it or don't, with the estimate and the reason."""

    ship: bool
    estimate: Estimate
    reason: str

    def summary(self) -> dict:
        return {"ship": self.ship, "reason": self.reason, **self.estimate.summary()}


def should_deploy(estimate: Estimate, *,
                  min_improvement: Decimal = Decimal("0.01")) -> Verdict:
    """Decide whether an offline estimate justifies exposing customers to a policy.

    Four ways to fail and one to pass, and the four are kept distinct because
    they call for different work: get coverage, get more data, get a better
    candidate, or accept that the current policy is fine.
    """
    if not isinstance(min_improvement, Decimal):
        raise OfflineEvalError("min_improvement must be a Decimal")
    if not estimate.identified:
        return Verdict(False, estimate,
                       "not identified — the log does not cover what the policy would do; "
                       "widen exploration before asking again")
    if estimate.concentrated:
        return Verdict(False, estimate,
                       f"estimate rests on too little effective data (ess {estimate.ess} "
                       f"of {estimate.n}) — collect more before acting on it")
    improvement = estimate.improvement_over_logged()
    if improvement < min_improvement:
        return Verdict(False, estimate,
                       f"estimated improvement {improvement} is below the "
                       f"{min_improvement} bar; the deployed policy is good enough")
    return Verdict(True, estimate,
                   f"estimated improvement {improvement} over the logged policy "
                   f"({estimate.logged_mean} -> {estimate.value}), identified and "
                   f"not weight-concentrated")
