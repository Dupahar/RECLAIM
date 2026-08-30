"""Funded-moment prediction — *when* to retry, not just whether.

Architecture §7: *"Funded-moment predictor — per-customer model of **when** an
account is likely funded (the Stripe Smart Retries insight: ML-timed retries on
billions of data points beat fixed schedules)."*

The engine currently retries on a fixed schedule: notice + 24h, then every
``gap_hours``. That schedule is indifferent to the one fact that decides whether
a debit succeeds — whether there is money in the account at that moment. In
India that fact is strongly cyclical: salary credits cluster in the first week of
the month, and a retry on the 28th against an account that funds on the 1st is a
contact spent on a near-certain decline.

**A concentration model, not a classifier.** For a customer, count the
day-of-month and hour-of-day of every moment money was actually there, take the
mode of each, and report *how concentrated* the history was as the confidence.
That is a real signal, it is explainable to a merchant in one sentence ("you get
paid on the 2nd"), and it needs a dozen observations rather than a billion. What
it is not is a learned model of anything richer; a production version would
condition on more than the calendar, at the cost of both properties.

**Hierarchical, again because low volume is the normal case.** Thin customer
history falls back to a pooled prior over every customer, then to nothing — and
every prediction names the level that answered it. A pooled prediction is
explicitly not a personal one.

**Confidence is concentration, and it is allowed to be low.** A customer whose
history is spread evenly across the month gets a prediction with a low
confidence and a note saying the history is diffuse. The caller can then decline
to re-time and fall back to the fixed schedule, which is the correct behaviour:
a timing model that cannot see a pattern must not pretend to.

**The compliance window always wins.** ``next_after`` returns the next
occurrence *strictly after* a supplied bound, so a caller passes the RBI notice
deadline and cannot get back a time before it. A predictor that could schedule a
debit inside the notice window would be a compliance bug wearing an ML hat.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Iterable, Optional

_Q = Decimal("0.0001")


class TimingError(Exception):
    """Raised on an invalid funded-moment history or prediction request."""


class Basis(str, Enum):
    CUSTOMER = "customer history"
    POOLED = "pooled prior across customers"
    NONE = "no evidence"


@dataclass(frozen=True)
class FundedMoment:
    """One observation that an account had money at a point in time.

    Sourced from successful collections and incoming credits — the events that
    prove funding rather than merely suggest it.
    """

    customer_id: str
    at: datetime

    def __post_init__(self) -> None:
        if not (isinstance(self.customer_id, str) and self.customer_id):
            raise TimingError("customer_id is required")
        if not isinstance(self.at, datetime):
            raise TimingError("at must be a datetime")


@dataclass(frozen=True)
class Prediction:
    """A predicted funded moment, with the evidence that produced it."""

    day_of_month: Optional[int]
    hour: Optional[int]
    day_confidence: Optional[Decimal]
    hour_confidence: Optional[Decimal]
    support: int
    basis: Basis
    note: str

    @property
    def is_known(self) -> bool:
        return self.basis is not Basis.NONE

    @property
    def is_personal(self) -> bool:
        """True only for a prediction from this customer's own history."""
        return self.basis is Basis.CUSTOMER

    def is_confident(self, threshold: Decimal = Decimal("0.40")) -> bool:
        """Whether the history was concentrated enough to act on.

        A diffuse history is not an error and not a secret: it produces a
        prediction whose confidence the caller is expected to check before
        preferring it to the fixed schedule.
        """
        if not isinstance(threshold, Decimal):
            raise TimingError("threshold must be a Decimal")
        if self.day_confidence is None:
            return False
        return self.day_confidence >= threshold

    def next_after(self, bound: datetime) -> Optional[datetime]:
        """The next predicted funded moment strictly after ``bound``.

        ``bound`` is where the caller puts the compliance floor — the RBI notice
        deadline — so the result can never precede it. A day-of-month that does
        not exist in the target month (the 31st of September) clamps to that
        month's last day rather than silently skipping a cycle.
        """
        if not isinstance(bound, datetime):
            raise TimingError("bound must be a datetime")
        if not self.is_known:
            return None
        year, month = bound.year, bound.month
        for _ in range(3):          # this month, next, and the one after
            last = calendar.monthrange(year, month)[1]
            day = min(self.day_of_month, last)
            candidate = datetime(year, month, day, self.hour, 0, 0,
                                 tzinfo=bound.tzinfo)
            if candidate > bound:
                return candidate
            month = 1 if month == 12 else month + 1
            year = year + 1 if month == 1 else year
        raise TimingError(  # pragma: no cover - unreachable: 3 months always contain one
            "could not place the predicted moment within three months")

    def summary(self) -> dict:
        def s(v):
            return None if v is None else str(v)
        return {"day_of_month": self.day_of_month, "hour": self.hour,
                "day_confidence": s(self.day_confidence),
                "hour_confidence": s(self.hour_confidence),
                "support": self.support, "basis": self.basis.value, "note": self.note}


_UNKNOWN = Prediction(None, None, None, None, 0, Basis.NONE,
                      "no funded moments observed for this customer or the pool")


def _mode(values) -> tuple[int, Decimal]:
    """Most common value and its share. Ties break to the lowest value, so the
    prediction is stable across runs (G4) rather than dict-order dependent."""
    counts: dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    best = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    share = (Decimal(best[1]) / Decimal(len(values))).quantize(_Q)
    return best[0], share


class FundedMomentPredictor:
    """Per-customer funded-moment model with a pooled fallback."""

    def __init__(self, history: dict, pooled: list, *, min_support: int = 6) -> None:
        self._history = history
        self._pooled = pooled
        self._min_support = min_support

    @property
    def min_support(self) -> int:
        return self._min_support

    @property
    def customer_count(self) -> int:
        return len(self._history)

    @property
    def pooled_support(self) -> int:
        return len(self._pooled)

    def support_for(self, customer_id: str) -> int:
        return len(self._history.get(customer_id, ()))

    def predict(self, customer_id: str) -> Prediction:
        if not (isinstance(customer_id, str) and customer_id):
            raise TimingError("customer_id is required")
        own = self._history.get(customer_id, [])
        if len(own) >= self._min_support:
            return self._from(own, Basis.CUSTOMER, customer_id)
        if len(self._pooled) >= self._min_support:
            return self._from(self._pooled, Basis.POOLED, customer_id,
                              own_support=len(own))
        return _UNKNOWN

    def _from(self, moments, basis: Basis, customer_id: str,
              own_support: Optional[int] = None) -> Prediction:
        day, day_share = _mode([m.at.day for m in moments])
        hour, hour_share = _mode([m.at.hour for m in moments])
        if basis is Basis.CUSTOMER:
            note = (f"{customer_id} was funded on day {day} in "
                    f"{day_share} of {len(moments)} observations")
        else:
            note = (f"{customer_id} has only {own_support} observation(s), below "
                    f"min_support={self._min_support}; using the pooled prior over "
                    f"{len(moments)} moments — this is not a personal prediction")
        if day_share < Decimal("0.40"):
            note += " | history is diffuse; prefer the fixed schedule"
        return Prediction(day_of_month=day, hour=hour, day_confidence=day_share,
                          hour_confidence=hour_share, support=len(moments),
                          basis=basis, note=note)


def fit(moments: Iterable[FundedMoment], *, min_support: int = 6) -> FundedMomentPredictor:
    """Build the predictor. Counting, not optimising: order-independent and
    reproducible."""
    if not isinstance(min_support, int) or isinstance(min_support, bool) or min_support < 1:
        raise TimingError("min_support must be a positive int")
    history: dict[str, list] = {}
    pooled: list = []
    for m in moments:
        if not isinstance(m, FundedMoment):
            raise TimingError("every observation must be a FundedMoment")
        history.setdefault(m.customer_id, []).append(m)
        pooled.append(m)
    return FundedMomentPredictor(history, pooled, min_support=min_support)


# --------------------------------------------------------------------------
# Choosing an attempt time — where the prediction meets the compliance floor
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Schedule:
    """When to attempt, and why that time was chosen."""

    at: datetime
    retimed: bool
    reason: str


def schedule_attempt(predictor: FundedMomentPredictor, customer_id: str, *,
                     notice_deadline: datetime, fallback: datetime,
                     confidence_threshold: Decimal = Decimal("0.40")) -> Schedule:
    """Pick an attempt time: the predicted funded moment, or the fixed schedule.

    ``notice_deadline`` is the earliest legal moment (base + notice hours);
    ``fallback`` is what the bounded engine would have done anyway. The
    prediction is only preferred when it is *known*, *confident*, and lands after
    the deadline — three conditions rather than one, because each failure mode
    (no data, diffuse data, illegal timing) has a different correct response and
    collapsing them would hide which one occurred.
    """
    for arg, name in ((notice_deadline, "notice_deadline"), (fallback, "fallback")):
        if not isinstance(arg, datetime):
            raise TimingError(f"{name} must be a datetime")
    if fallback < notice_deadline:
        raise TimingError("fallback is before the notice deadline; the compliance "
                          "window is not the timing model's to overrule")

    prediction = predictor.predict(customer_id)
    if not prediction.is_known:
        return Schedule(fallback, False,
                        f"fixed schedule: {prediction.note}")
    if not prediction.is_confident(confidence_threshold):
        return Schedule(fallback, False,
                        f"fixed schedule: prediction confidence "
                        f"{prediction.day_confidence} is below {confidence_threshold}")
    predicted = prediction.next_after(notice_deadline)
    return Schedule(predicted, True,
                    f"re-timed to the predicted funded moment ({prediction.basis.value}): "
                    f"{prediction.note}")
