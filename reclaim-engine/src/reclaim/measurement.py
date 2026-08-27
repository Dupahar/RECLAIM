"""Causal measurement — goal G9, "recovery impact measured against a control".

Architecture §9.2. The number a recovery product is tempted to report is *gross*
recovered: "we won back ₹63,700." That number is close to meaningless, because
some of that money would have arrived anyway — a customer tops up and the debit
succeeds on its own. What a merchant actually buys is the **incremental** part.

So this module measures the only thing that is honest to claim:

    lift = recovery rate (treated) - recovery rate (control)

and turns it into rupees the intervention can be credited with.

**Deterministic holdout, not randomness.** The engine forbids ``random`` and
``now()`` (G4), and an experiment assignment that changes between runs would be
unreplayable anyway. Cohorts are assigned by hashing ``salt + unit_id``, which
is stable, uniformly distributed, reproducible on any machine, and independent
across experiments because the salt changes with each one.

**What this module refuses to do.** With an empty control group it reports
``None``, not a lift of 100%. With a cohort below ``min_cohort`` it sets
``underpowered``. It never invents a counterfactual: control outcomes must be
*observed* — a control leak that resolved on its own is visible when the next
batch re-reconciles — and the caller supplies those observations. A lift number
computed from a control group that was never really observed would be exactly
the kind of assertion this project exists to avoid.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

from .money import Money

_Q = Decimal("0.0001")
_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


class MeasurementError(Exception):
    """Raised on an invalid experiment configuration or mismatched observations."""


class Arm(str, Enum):
    TREATED = "treated"    # the recovery workflow ran
    CONTROL = "control"    # deliberately left alone, to measure what we add


@dataclass(frozen=True)
class HoldoutPolicy:
    """Deterministic control-group assignment for one experiment.

    ``control_pct`` of units are held back and never acted on. That is a real
    cost — money the engine could have chased and did not — and it is the price
    of being able to say anything truthful about impact.
    """

    control_pct: int = 10
    salt: str = "reclaim-recovery-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.control_pct, int) or isinstance(self.control_pct, bool):
            raise MeasurementError("control_pct must be an int")
        if not (0 <= self.control_pct <= 100):
            raise MeasurementError("control_pct must be within 0..100")
        if not isinstance(self.salt, str) or self.salt == "":
            raise MeasurementError("salt must be a non-empty string")

    def bucket(self, unit_id: str) -> int:
        """Stable 0..99 bucket for a unit. Same input, same bucket, forever."""
        if not isinstance(unit_id, str) or unit_id == "":
            raise MeasurementError("unit_id must be a non-empty string")
        digest = hashlib.sha256(f"{self.salt}:{unit_id}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % 100

    def arm(self, unit_id: str) -> Arm:
        return Arm.CONTROL if self.bucket(unit_id) < self.control_pct else Arm.TREATED


@dataclass(frozen=True)
class Observation:
    """One unit's outcome: was the money eventually recovered, or not?"""

    unit_id: str
    arm: Arm
    amount: Money
    recovered: bool

    def __post_init__(self) -> None:
        if not isinstance(self.unit_id, str) or self.unit_id == "":
            raise MeasurementError("unit_id must be a non-empty string")
        if not isinstance(self.arm, Arm):
            raise MeasurementError("arm must be an Arm")
        if not isinstance(self.amount, Money):
            raise MeasurementError("amount must be Money")
        if not isinstance(self.recovered, bool):
            raise MeasurementError("recovered must be a bool")


@dataclass(frozen=True)
class CohortResult:
    arm: Arm
    n: int
    recovered_n: int
    recovered_amount: Money
    total_amount: Money

    def rate(self) -> Optional[Decimal]:
        """Share of units recovered, or None for an empty cohort."""
        if self.n == 0:
            return None
        return (Decimal(self.recovered_n) / Decimal(self.n)).quantize(_Q)

    def amount_rate(self) -> Optional[Decimal]:
        """Share of *value* recovered, or None when there is nothing at stake."""
        if self.total_amount.is_zero:
            return None
        return (self.recovered_amount.amount / self.total_amount.amount).quantize(_Q)


@dataclass(frozen=True)
class CausalLift:
    treated: CohortResult
    control: CohortResult
    lift_pp: Optional[Decimal]          # percentage points, treated rate - control rate
    incremental_amount: Optional[Money]  # rupees creditable to the intervention
    underpowered: bool
    note: str

    def is_measurable(self) -> bool:
        return self.lift_pp is not None

    def summary(self) -> dict:
        return {
            "treated_n": self.treated.n,
            "control_n": self.control.n,
            "treated_rate": str(self.treated.rate()) if self.treated.rate() is not None else None,
            "control_rate": str(self.control.rate()) if self.control.rate() is not None else None,
            "lift_pp": str(self.lift_pp) if self.lift_pp is not None else None,
            "gross_recovered": str(self.treated.recovered_amount),
            "incremental_recovered": (str(self.incremental_amount)
                                      if self.incremental_amount is not None else None),
            "underpowered": self.underpowered,
            "note": self.note,
        }


def _cohort(observations, arm: Arm, currency: str) -> CohortResult:
    members = [o for o in observations if o.arm is arm]
    recovered = [o for o in members if o.recovered]
    total = Money.zero(currency)
    got = Money.zero(currency)
    for o in members:
        total = total + o.amount
    for o in recovered:
        got = got + o.amount
    return CohortResult(arm=arm, n=len(members), recovered_n=len(recovered),
                        recovered_amount=got, total_amount=total)


def measure_lift(observations, *, min_cohort: int = 30) -> CausalLift:
    """Causal lift of the recovery workflow over a do-nothing control.

    ``incremental_amount`` is the treated recovery minus what the treated cohort
    would be expected to recover with no intervention at all, estimated from the
    control cohort's own value-recovery rate. That subtraction is the entire
    point: it is what stops a self-healing payment from being claimed as a win.
    """
    observations = list(observations)
    if not observations:
        raise MeasurementError("cannot measure lift with no observations")
    currencies = {o.amount.currency for o in observations}
    if len(currencies) > 1:
        raise MeasurementError(f"observations span multiple currencies: {sorted(currencies)}")
    currency = currencies.pop()

    treated = _cohort(observations, Arm.TREATED, currency)
    control = _cohort(observations, Arm.CONTROL, currency)

    if treated.n == 0 or control.n == 0:
        missing = "treated" if treated.n == 0 else "control"
        return CausalLift(
            treated=treated, control=control, lift_pp=None, incremental_amount=None,
            underpowered=True,
            note=f"no {missing} cohort — lift is undefined, not zero; "
                 "gross recovery cannot be credited to the intervention",
        )

    lift_pp = ((treated.rate() - control.rate()) * _HUNDRED).quantize(_Q)

    control_value_rate = control.amount_rate()
    if control_value_rate is None:
        incremental = treated.recovered_amount     # control had nothing at stake
        note = "control cohort had zero value at stake; incremental equals gross"
    else:
        # Round the counterfactual to currency minor units: it is an estimate
        # reported as money, and sub-paisa precision would imply a confidence
        # the estimate does not have.
        counterfactual = (treated.total_amount * control_value_rate).round()
        incremental = treated.recovered_amount - counterfactual
        note = (f"counterfactual {counterfactual} of {treated.total_amount} at stake, "
                f"from a control value-recovery rate of {control_value_rate}")

    underpowered = treated.n < min_cohort or control.n < min_cohort
    if underpowered:
        note += (f" | UNDERPOWERED: cohorts of {treated.n}/{control.n} are below "
                 f"min_cohort={min_cohort}; treat the lift as indicative, not established")

    return CausalLift(treated=treated, control=control, lift_pp=lift_pp,
                      incremental_amount=incremental, underpowered=underpowered, note=note)
