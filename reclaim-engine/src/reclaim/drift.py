"""Drift monitoring — noticing when a policy that was right stops being right.

Architecture §9.2 asks for drift monitors alongside the offline evaluators. The
gap they close is the one every learning system eventually walks into: a policy
is validated once, deployed, and then quietly decays as customer behaviour,
channel deliverability or the merchant mix changes. Nothing in the engine looked
again after deployment. A policy that was right last month and wrong this month
produced exactly the same logs as one that was still right.

Two independent signals, because they fail in that order:

**Reward drift** — did the realised outcome rate fall? This is what matters, and
it is also the *last* thing to move, because it needs enough failures to
accumulate before it clears the noise.

**Action-mix drift** — did the distribution over chosen actions shift? An
ε-greedy policy that collapses onto one arm, or starts spraying exploration
because every arm looks equally bad, changes its mix well before its reward
becomes distinguishable. Reported separately rather than folded into one score,
because a mix shift with stable reward is a different situation (the policy found
a new equilibrium) from a mix shift with falling reward (it is thrashing).

**The statistics are deliberately plain.** A pooled two-proportion z-test, and a
total-variation distance for the mix. Both are computed in ``Decimal`` with no
dependencies, both are reproducible, and both are the kind of arithmetic a
reviewer can check by hand. Anything fancier would be harder to justify than the
decision it informs.

**It refuses on thin data, loudly.** A window below ``min_n`` yields
``INSUFFICIENT_DATA``, not a verdict. The whole point of a monitor is to be
believed when it fires, and a monitor that cries drift on twelve observations
gets muted — after which it is worse than nothing, because its silence is now
also uninformative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Iterable, Optional

from .offline_eval import LoggedDecision

_Q = Decimal("0.0001")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")


class DriftError(Exception):
    """Raised when a drift comparison cannot be made honestly."""


class DriftVerdict(str, Enum):
    STABLE = "stable"
    DEGRADED = "degraded"
    IMPROVED = "improved"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class WindowStats:
    """One window's realised behaviour: what happened, and what was chosen."""

    n: int
    reward_total: Decimal
    binary: bool
    action_counts: dict = field(default_factory=dict)

    @property
    def mean(self) -> Optional[Decimal]:
        if self.n == 0:
            return None
        return (self.reward_total / Decimal(self.n)).quantize(_Q)

    @property
    def successes(self) -> Optional[int]:
        """Only meaningful for 0/1 rewards, which is what the z-test needs."""
        return int(self.reward_total) if self.binary else None

    def action_mix(self) -> dict:
        """Share of decisions per action, in sorted key order for determinism."""
        if self.n == 0:
            return {}
        return {key: (Decimal(count) / Decimal(self.n)).quantize(_Q)
                for key, count in sorted(self.action_counts.items())}

    def summary(self) -> dict:
        return {"n": self.n, "mean": str(self.mean) if self.mean is not None else None,
                "binary": self.binary,
                "action_mix": {k: str(v) for k, v in self.action_mix().items()}}


def summarise(log: Iterable[LoggedDecision]) -> WindowStats:
    """Reduce a log window to the statistics a comparison needs."""
    rows = list(log)
    for row in rows:
        if not isinstance(row, LoggedDecision):
            raise DriftError("every log row must be a LoggedDecision")
    total = sum((r.reward for r in rows), _ZERO)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.action_key] = counts.get(row.action_key, 0) + 1
    binary = all(r.reward in (_ZERO, _ONE) for r in rows)
    return WindowStats(n=len(rows), reward_total=total, binary=binary,
                       action_counts=counts)


@dataclass(frozen=True)
class DriftReport:
    reference: WindowStats
    current: WindowStats
    verdict: DriftVerdict
    delta: Optional[Decimal]
    z: Optional[Decimal]
    mix_shift: Optional[Decimal]
    note: str

    @property
    def is_actionable(self) -> bool:
        """True only for a verdict a human should do something about."""
        return self.verdict is DriftVerdict.DEGRADED

    @property
    def mix_shifted(self) -> bool:
        return self.mix_shift is not None and self.mix_shift >= Decimal("0.20")

    def summary(self) -> dict:
        def s(v):
            return None if v is None else str(v)
        return {"verdict": self.verdict.value, "delta": s(self.delta), "z": s(self.z),
                "mix_shift": s(self.mix_shift), "mix_shifted": self.mix_shifted,
                "actionable": self.is_actionable, "note": self.note,
                "reference": self.reference.summary(),
                "current": self.current.summary()}


def _total_variation(a: dict, b: dict) -> Decimal:
    """Half the L1 distance between two action mixes: 0 identical, 1 disjoint."""
    keys = set(a) | set(b)
    if not keys:
        return _ZERO
    gap = sum((abs(a.get(k, _ZERO) - b.get(k, _ZERO)) for k in keys), _ZERO)
    return (gap / _TWO).quantize(_Q)


def _z_score(reference: WindowStats, current: WindowStats) -> Optional[Decimal]:
    """Pooled two-proportion z. ``None`` when it is not defined.

    Undefined in two cases, both reported rather than papered over: non-binary
    rewards (the test assumes Bernoulli trials), and a pooled rate of exactly 0
    or 1, where there is no variance to compare against because every
    observation in both windows agreed.
    """
    if not (reference.binary and current.binary):
        return None
    n_ref, n_cur = Decimal(reference.n), Decimal(current.n)
    pooled = (reference.reward_total + current.reward_total) / (n_ref + n_cur)
    if pooled == _ZERO or pooled == _ONE:
        return None
    variance = pooled * (_ONE - pooled) * (_ONE / n_ref + _ONE / n_cur)
    return ((current.mean - reference.mean) / variance.sqrt()).quantize(_Q)


def detect_drift(reference: Iterable[LoggedDecision], current: Iterable[LoggedDecision],
                 *, min_n: int = 30,
                 z_threshold: Decimal = _TWO) -> DriftReport:
    """Compare a reference window to a current one.

    ``z_threshold`` defaults to 2, roughly a 95% two-sided call. It is a
    deliberate choice rather than a convention: a monitor on money actions should
    fire before a merchant notices, and the cost of a false alarm here is a human
    looking at a dashboard.
    """
    if not isinstance(min_n, int) or isinstance(min_n, bool) or min_n < 1:
        raise DriftError("min_n must be a positive int")
    if not isinstance(z_threshold, Decimal) or z_threshold <= _ZERO:
        raise DriftError("z_threshold must be a positive Decimal")

    ref, cur = summarise(reference), summarise(current)
    mix_shift = _total_variation(ref.action_mix(), cur.action_mix())

    if ref.n < min_n or cur.n < min_n:
        return DriftReport(
            reference=ref, current=cur, verdict=DriftVerdict.INSUFFICIENT_DATA,
            delta=None, z=None, mix_shift=mix_shift,
            note=(f"windows of {ref.n}/{cur.n} are below min_n={min_n}; a monitor that "
                  "fires on thin data gets muted, after which its silence is "
                  "uninformative too"))

    delta = (cur.mean - ref.mean).quantize(_Q)
    z = _z_score(ref, cur)

    if z is None:
        reason = ("rewards are not 0/1, so the two-proportion test does not apply"
                  if not (ref.binary and cur.binary)
                  else "every observation in both windows agreed, so there is no "
                       "variance to test against")
        verdict = DriftVerdict.STABLE
        note = f"delta {delta} reported without a significance test: {reason}"
    elif z <= -z_threshold:
        verdict = DriftVerdict.DEGRADED
        note = (f"reward fell from {ref.mean} to {cur.mean} (delta {delta}, z {z}); "
                f"past the -{z_threshold} threshold — re-evaluate the policy against "
                "the recent log before it keeps running")
    elif z >= z_threshold:
        verdict = DriftVerdict.IMPROVED
        note = (f"reward rose from {ref.mean} to {cur.mean} (delta {delta}, z {z}); "
                "not a problem, but the reference window is now stale")
    else:
        verdict = DriftVerdict.STABLE
        note = (f"delta {delta} with z {z} is inside +/-{z_threshold}; consistent with "
                "noise")

    if mix_shift >= Decimal("0.20"):
        note += (f" | ACTION MIX SHIFTED: total-variation {mix_shift} — the policy is "
                 "choosing differently, which usually precedes a reward change")
    return DriftReport(reference=ref, current=cur, verdict=verdict, delta=delta,
                       z=z, mix_shift=mix_shift, note=note)


def windows(log: Iterable[LoggedDecision], size: int) -> tuple[tuple[LoggedDecision, ...], ...]:
    """Split a log into consecutive windows of ``size``, dropping any remainder.

    The remainder is dropped rather than returned short, so every window a
    caller compares has the same weight. A trailing partial window is exactly
    the one most likely to produce a spurious verdict.
    """
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise DriftError("size must be a positive int")
    rows = list(log)
    for row in rows:
        if not isinstance(row, LoggedDecision):
            raise DriftError("every log row must be a LoggedDecision")
    return tuple(tuple(rows[i:i + size])
                 for i in range(0, len(rows) - size + 1, size))


def scan(log: Iterable[LoggedDecision], *, size: int, min_n: int = 30,
         z_threshold: Decimal = _TWO) -> tuple[DriftReport, ...]:
    """Walk a log window by window, comparing each to the first.

    Comparing to the *first* window rather than the previous one is deliberate:
    a slow decay would pass a previous-window comparison every time while the
    policy quietly halved. A fixed reference makes cumulative drift visible.
    """
    chunks = windows(log, size)
    if len(chunks) < 2:
        raise DriftError(
            f"need at least two windows of {size} to compare; got {len(chunks)}")
    return tuple(detect_drift(chunks[0], chunk, min_n=min_n, z_threshold=z_threshold)
                 for chunk in chunks[1:])


__all__ = ["DriftError", "DriftReport", "DriftVerdict", "WindowStats",
           "detect_drift", "scan", "summarise", "windows"]
