"""Contextual bandit — learning the best (timing × channel × message) per context.

Architecture §7: *"Contextual bandit (Thompson sampling) — learns the best
`(timing × channel × message)` per context, balancing exploration/exploitation,
with **offline policy evaluation via Inverse Propensity Scoring / Doubly Robust**
so we can validate a new policy *before* deploying it."*

Uplift ([ADR-0024](../docs/decisions/ADR-0024-uplift-targeting.md)) decides
*whether* to contact; the funded-moment predictor decides *when*. This decides
*how*: which channel, which message, which hour, given what we know. It is the
last learning component in Layer 5.

**ε-greedy, not Thompson sampling — and the reason is the second half of the
requirement.** Offline evaluation by IPS or DR divides each logged reward by the
probability the logging policy had of choosing that action. Thompson sampling's
action probabilities are an integral over the posterior; they have to be
*estimated*, and an estimated propensity in the denominator of an IPS estimator
introduces bias that cannot be bounded from the log itself. ε-greedy's
propensities are exact and closed-form: ``1 - ε + ε/K`` for the greedy action,
``ε/K`` for every other. Since the architecture asks for the evaluator as well as
the sampler, the sampler that makes the evaluator honest wins. Recorded as a
deviation in ADR-0027.

**Deterministic exploration.** ``random`` is forbidden (G4) and an exploration
draw that changed between runs would make a run unreplayable. The draw is a hash
of ``salt + context + unit``, exactly as the holdout assigns cohorts — uniform,
reproducible on any machine, and independent of the holdout because the salt
differs.

**Every decision logs its own propensity.** ``Choice.propensity`` is written at
decision time, not reconstructed later. A propensity reconstructed after the
policy has been updated is a different number from the one that was actually
used, and the difference is invisible in the result.

**Cold arms are explored, not assumed.** An arm with no observations has a Beta
prior mean of ½ rather than 0, so the policy does not permanently avoid an action
it has never tried. Pessimism about the untried looks like learning and is
actually a self-fulfilling prophecy.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from .recovery import Channel

_Q = Decimal("0.0001")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class BanditError(Exception):
    """Raised on an invalid bandit configuration or update."""


@dataclass(frozen=True)
class Action:
    """One thing the engine can do: a channel, an hour, and a message template."""

    channel: Channel
    hour: int
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.channel, Channel):
            raise BanditError("channel must be a Channel")
        if not isinstance(self.hour, int) or isinstance(self.hour, bool):
            raise BanditError("hour must be an int")
        if not (0 <= self.hour <= 23):
            raise BanditError("hour must be within 0..23")
        if not (isinstance(self.message, str) and self.message):
            raise BanditError("message must be a non-empty template id")

    @property
    def key(self) -> str:
        """Stable identifier, used as the log key and for deterministic ordering."""
        return f"{self.channel.value}|{self.hour:02d}|{self.message}"


@dataclass(frozen=True)
class ArmStats:
    """Beta posterior over one arm's success rate, as counts.

    A Beta(1,1) prior — uniform — means an untried arm reads as ½ rather than 0.
    """

    successes: int = 0
    failures: int = 0

    def __post_init__(self) -> None:
        for name, val in (("successes", self.successes), ("failures", self.failures)):
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                raise BanditError(f"{name} must be a non-negative int")

    @property
    def n(self) -> int:
        return self.successes + self.failures

    @property
    def mean(self) -> Decimal:
        """Posterior mean, ``(s+1)/(n+2)``. Never 0 or 1, so no arm is written off
        or treated as certain on finite evidence."""
        return (Decimal(self.successes + 1) / Decimal(self.n + 2)).quantize(_Q)

    def plus(self, reward: bool) -> "ArmStats":
        if not isinstance(reward, bool):
            raise BanditError("reward must be a bool")
        return (ArmStats(self.successes + 1, self.failures) if reward
                else ArmStats(self.successes, self.failures + 1))


@dataclass(frozen=True)
class Choice:
    """A chosen action, with the propensity that must be logged alongside it."""

    action: Action
    propensity: Decimal
    explored: bool
    greedy_action: Action
    context_key: str

    @property
    def is_greedy(self) -> bool:
        return self.action == self.greedy_action

    def summary(self) -> dict:
        return {"context": self.context_key, "action": self.action.key,
                "propensity": str(self.propensity), "explored": self.explored,
                "greedy": self.greedy_action.key}


@dataclass(frozen=True)
class EpsilonGreedyConfig:
    """Exploration rate and its bound.

    ``epsilon`` is the share of decisions that deliberately try a non-greedy
    action. It is capped low by default: exploration here means sending a real
    customer a message the policy believes is worse, which is a cost paid in
    someone's inbox, not just in regret.
    """

    epsilon: Decimal = Decimal("0.10")
    salt: str = "reclaim-bandit-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.epsilon, Decimal):
            raise BanditError("epsilon must be a Decimal")
        if not (_ZERO < self.epsilon <= Decimal("0.5")):
            raise BanditError("epsilon must be within (0, 0.5]; exploration is a cost "
                              "paid in a real customer's inbox, so it is capped")
        if not (isinstance(self.salt, str) and self.salt):
            raise BanditError("salt must be a non-empty string")


DEFAULT_CONFIG = EpsilonGreedyConfig()


class EpsilonGreedyBandit:
    """Contextual ε-greedy policy with exact, loggable propensities."""

    def __init__(self, actions: Iterable[Action], stats: Optional[dict] = None, *,
                 config: EpsilonGreedyConfig = DEFAULT_CONFIG) -> None:
        actions = tuple(actions)
        if not actions:
            raise BanditError("a bandit needs at least one action")
        if not all(isinstance(a, Action) for a in actions):
            raise BanditError("every action must be an Action")
        keys = [a.key for a in actions]
        if len(set(keys)) != len(keys):
            raise BanditError("action keys must be unique")
        # Sorted, so the greedy tie-break and the exploration index are stable
        # regardless of the order the caller supplied.
        self._actions = tuple(sorted(actions, key=lambda a: a.key))
        self._stats: dict[tuple[str, str], ArmStats] = dict(stats or {})
        self._config = config

    # ---- introspection ------------------------------------------------
    @property
    def actions(self) -> tuple[Action, ...]:
        return self._actions

    @property
    def k(self) -> int:
        return len(self._actions)

    @property
    def config(self) -> EpsilonGreedyConfig:
        return self._config

    def stats_for(self, context_key: str, action: Action) -> ArmStats:
        return self._stats.get((context_key, action.key), ArmStats())

    def table(self, context_key: str) -> dict:
        """Every arm's posterior mean in one context — the auditable artifact."""
        return {a.key: self.stats_for(context_key, a).mean for a in self._actions}

    # ---- the policy ---------------------------------------------------
    def greedy(self, context_key: str) -> Action:
        """Best arm by posterior mean.

        ``_actions`` is sorted by key and ``max`` returns the first maximal
        element, so a tie breaks to the lowest key without any extra machinery —
        deterministic (G4) and independent of the order the caller supplied.
        """
        return max(self._actions, key=lambda a: self.stats_for(context_key, a).mean)

    def propensity(self, context_key: str, action: Action) -> Decimal:
        """P(choosing ``action`` in this context). Exact, not estimated.

        This is the number IPS and DR divide by, which is why it is a first-class
        method rather than a by-product of ``choose``: an evaluator must be able
        to ask what the probability *would have been* for an action that was not
        taken.
        """
        if action not in self._actions:
            raise BanditError(f"unknown action {action.key!r}")
        # The non-greedy share is quantized first and the greedy arm absorbs the
        # remainder, so the distribution sums to exactly 1. Quantizing each arm
        # independently leaves it summing to 0.9999 for k=3 -- small, but it
        # would make every importance-weighted estimate a weighted sum against a
        # distribution that is not one, which is a bias with no upper bound as
        # weights grow.
        share = (self._config.epsilon / Decimal(self.k)).quantize(_Q)
        if action == self.greedy(context_key):
            return _ONE - Decimal(self.k - 1) * share
        return share

    def choose(self, context_key: str, unit_id: str) -> Choice:
        """Pick an action, deterministically, and report its exact propensity."""
        if not (isinstance(context_key, str) and context_key):
            raise BanditError("context_key must be a non-empty string")
        if not (isinstance(unit_id, str) and unit_id):
            raise BanditError("unit_id must be a non-empty string")
        greedy = self.greedy(context_key)
        draw, index = self._draw(context_key, unit_id)
        # Scale epsilon to the integer draw space once, rather than comparing a
        # Decimal to a ratio of ints.
        explore = draw < int(self._config.epsilon * 10_000)
        action = self._actions[index] if explore else greedy
        return Choice(action=action,
                      propensity=self.propensity(context_key, action),
                      explored=explore and action != greedy,
                      greedy_action=greedy, context_key=context_key)

    def _draw(self, context_key: str, unit_id: str) -> tuple[int, int]:
        """Two deterministic values: whether to explore, and which arm to try."""
        digest = hashlib.sha256(
            f"{self._config.salt}:{context_key}:{unit_id}".encode("utf-8")).digest()
        return (int.from_bytes(digest[:8], "big") % 10_000,
                int.from_bytes(digest[8:16], "big") % self.k)

    # ---- learning -----------------------------------------------------
    def updated(self, context_key: str, action: Action, reward: bool) -> "EpsilonGreedyBandit":
        """A new bandit with one observation folded in.

        Returns a new instance rather than mutating: a policy that changes under
        a caller holding a reference to it would make the logged propensities
        untrustworthy, which is the one thing this module must not do.
        """
        if action not in self._actions:
            raise BanditError(f"unknown action {action.key!r}")
        key = (context_key, action.key)
        stats = dict(self._stats)
        stats[key] = self._stats.get(key, ArmStats()).plus(reward)
        return EpsilonGreedyBandit(self._actions, stats, config=self._config)

    def observe_all(self, observations) -> "EpsilonGreedyBandit":
        """Fold in many observations of ``(context_key, action, reward)``."""
        policy = self
        for context_key, action, reward in observations:
            policy = policy.updated(context_key, action, reward)
        return policy


# --------------------------------------------------------------------------
# Deterministic target policies, for offline evaluation
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class GreedyPolicy:
    """The deterministic policy "always take this bandit's greedy arm".

    This is the candidate an offline evaluator is usually asked about: *if we
    stopped exploring and always took the current best, what would we get?*
    """

    bandit: EpsilonGreedyBandit

    def probability(self, context_key: str, action_key: str) -> Decimal:
        return _ONE if self.bandit.greedy(context_key).key == action_key else _ZERO


@dataclass(frozen=True)
class FixedActionPolicy:
    """"Always send this one action" — the baseline a bandit has to beat."""

    action_key: str

    def probability(self, context_key: str, action_key: str) -> Decimal:
        return _ONE if action_key == self.action_key else _ZERO


@dataclass(frozen=True)
class LoggingPolicyEcho:
    """A policy that reproduces the logged propensities exactly.

    Evaluating it must return the log's own average reward. That identity is the
    sanity check every IPS implementation should be made to pass before anyone
    believes a number it produces about a *different* policy.
    """

    bandit: EpsilonGreedyBandit

    def probability(self, context_key: str, action_key: str) -> Decimal:
        for action in self.bandit.actions:
            if action.key == action_key:
                return self.bandit.propensity(context_key, action)
        return _ZERO  # pragma: no cover - logs only contain known actions
