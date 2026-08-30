"""The T+1 observation loop — where a claimed recovery becomes an observed one.

Architecture §9.2. Phase 22 built the holdout and the lift arithmetic, and then
stopped at the honest wall: cohorts were *assigned*, but nobody ever went back
to see what happened to them. Both halves of an experiment were missing their
outcome. A control leak that quietly resolved on its own was invisible, and a
treated leak the engine declared ``RECOVERED`` was taken at the engine's word.

This module closes that loop by re-reconciling a **later batch** and reading the
outcome out of the data instead of out of the engine's own claim.

Two design commitments, both deliberately uncomfortable:

**Only observed resolution counts.** ``RecoveryOutcome.RECOVERED`` means "the
executor returned success", which is a statement about an API call, not about
money in a bank account. This module ignores it when scoring the experiment and
looks only at whether the follow-up batch still shows the leak. That is what
makes the treated and control arms *symmetric* — both are judged by the same
evidence — and symmetric arms are the only thing that makes a difference between
them mean anything. The engine's claim is not discarded: where it disagrees with
the follow-up batch the unit is reported in ``claimed_not_observed``, which is a
recovery product auditing its own success metric.

**Intention-to-treat, not per-protocol.** A treated unit that halted for a
human, hit the AFA ceiling, or exhausted its attempts stays in the treated
cohort with ``recovered=False``. Dropping it would measure "how well recovery
works when it runs", which is a number you can improve by refusing to run — the
exact gaming the scorecard exists to prevent.

**What the follow-up batch must contain.** Reconciliation rolls open items
forward: the T+1 file re-presents settlements that had not tied out, alongside
the *cumulative* bank credits against them. So a ₹200 shortfall that was later
topped up appears at T+1 as a settlement whose credit now matches in full, and
one that was not appears again as the same leak id. Leak ids are pure functions
of the settlement id (``leak:short:s2``), so a unit's identity is stable across
runs and this comparison is exact rather than heuristic. A unit whose settlement
is absent from the follow-up batch is **not** scored as resolved — it is
reported as unobserved, because "we stopped looking" and "the money arrived" are
different facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .domain import LeakRecord, LeakType, RecoveryState
from .measurement import Arm, CausalLift, MeasurementError, Observation, measure_lift


class ObservationError(Exception):
    """Raised when a follow-up observation cannot be formed honestly."""


# Reason codes for a unit that could not be scored. Strings, not free text, so
# a caller can branch on them and a report can be totalled by reason.
NOT_CARRIED_FORWARD = "settlement absent from the follow-up batch; outcome unknown"
CURRENCY_MISMATCH = "follow-up batch is in a different currency; not comparable"


@dataclass(frozen=True)
class ObservedUnit:
    """One leak under experimental observation, with the arm it was really in.

    The arm is taken from what the prior run *did* — control units are the ones
    the pipeline actually held back — not re-derived from a policy object. A
    policy passed in after the fact could disagree with the run, and the run is
    the ground truth about which units were left alone.
    """

    leak: LeakRecord
    arm: Arm
    engine_claimed_recovered: bool

    def __post_init__(self) -> None:
        if not isinstance(self.leak, LeakRecord):
            raise ObservationError("leak must be a LeakRecord")
        if not isinstance(self.arm, Arm):
            raise ObservationError("arm must be an Arm")
        if not isinstance(self.engine_claimed_recovered, bool):
            raise ObservationError("engine_claimed_recovered must be a bool")


@dataclass(frozen=True)
class Unobserved:
    """A unit deliberately excluded from the measurement, with the reason."""

    leak_id: str
    arm: Arm
    reason: str


@dataclass(frozen=True)
class ObservationReport:
    observations: tuple[Observation, ...]
    unobserved: tuple[Unobserved, ...]
    claimed_not_observed: tuple[str, ...]

    @property
    def observed_count(self) -> int:
        return len(self.observations)

    @property
    def unobserved_count(self) -> int:
        return len(self.unobserved)

    def by_arm(self, arm: Arm) -> tuple[Observation, ...]:
        return tuple(o for o in self.observations if o.arm is arm)

    def is_measurable(self) -> bool:
        """True only when both arms have at least one *observed* unit."""
        return bool(self.by_arm(Arm.TREATED)) and bool(self.by_arm(Arm.CONTROL))

    def measure(self, *, min_cohort: int = 30) -> Optional[CausalLift]:
        """The causal lift, or ``None`` when there is nothing honest to report.

        Returning ``None`` rather than a zero or a placeholder is the whole
        posture of this module: an unmeasurable experiment must not render as a
        measured one.
        """
        if not self.observations:
            return None
        return measure_lift(self.observations, min_cohort=min_cohort)

    def summary(self) -> dict:
        return {
            "observed": self.observed_count,
            "treated_observed": len(self.by_arm(Arm.TREATED)),
            "control_observed": len(self.by_arm(Arm.CONTROL)),
            "unobserved": self.unobserved_count,
            "unobserved_reasons": {u.leak_id: u.reason for u in self.unobserved},
            "claimed_not_observed": list(self.claimed_not_observed),
            "measurable": self.is_measurable(),
        }


# --------------------------------------------------------------------------
# Reading a run: what it covered, and what it left open
# --------------------------------------------------------------------------
def covered_settlement_ids(run) -> frozenset[str]:
    """Settlement ids the batch actually contained.

    Every settlement in a batch ends up either in a matched pair or in a leak
    that names it, so this is a complete census. ``TIMING`` leaks are skipped:
    their ``source_refs[0]`` is a *bank* credit id, not a settlement.
    """
    ids = {m.settlement.id for m in run.exact.matched}
    for leak in run.exact.leaks:
        if leak.leak_type is LeakType.TIMING:
            continue
        if leak.source_refs:
            ids.add(leak.source_refs[0])
    return frozenset(ids)


def open_leak_ids(run) -> frozenset[str]:
    """Leak ids the run still reports as unresolved — its honest residual."""
    return frozenset(leak.id for leak in run.residual_leaks)


def units_under_observation(run) -> tuple[ObservedUnit, ...]:
    """The recoverable leaks a prior run put into the experiment.

    A leak qualifies when the run either held it back (control), ran recovery on
    it (treated), or left it open (treated but unresolved). Leaks a later fuzzy
    or AI match superseded are excluded — they were never missing money, so
    including them would dilute both cohorts with units that had nothing to
    recover.

    Order follows ``exact.leaks`` so the result is deterministic (G4).
    """
    control_ids = {leak.id for leak in run.control_leaks}
    claimed = {r.leak.id for r in run.recoveries
               if r.final_state is RecoveryState.RECOVERED}
    attempted_ids = {r.leak.id for r in run.recoveries}
    residual_ids = open_leak_ids(run)

    units: list[ObservedUnit] = []
    for leak in run.exact.leaks:
        if not leak.recoverable:
            continue
        in_experiment = (leak.id in control_ids
                         or leak.id in attempted_ids
                         or leak.id in residual_ids)
        if not in_experiment:
            continue                      # superseded by a later match
        arm = Arm.CONTROL if leak.id in control_ids else Arm.TREATED
        units.append(ObservedUnit(leak=leak, arm=arm,
                                  engine_claimed_recovered=leak.id in claimed))
    return tuple(units)


# --------------------------------------------------------------------------
# The loop itself
# --------------------------------------------------------------------------
def observe_followup(prior, followup) -> ObservationReport:
    """Score a prior run's experiment against a later batch's reconciliation.

    ``prior`` is the run that assigned cohorts and (for treated units) acted.
    ``followup`` is a later ``RunReport`` over a batch that re-presents the open
    items. A unit counts as recovered when the follow-up run no longer lists its
    leak as residual — money observed, not money claimed.
    """
    units = units_under_observation(prior)
    if not units:
        raise ObservationError(
            "prior run has no recoverable leaks under observation; there is no "
            "experiment to score")

    covered = covered_settlement_ids(followup)
    still_open = open_leak_ids(followup)
    followup_currency = followup.currency

    observations: list[Observation] = []
    unobserved: list[Unobserved] = []
    claimed_not_observed: list[str] = []

    for unit in units:
        leak = unit.leak
        if leak.amount.currency != followup_currency:
            unobserved.append(Unobserved(leak.id, unit.arm, CURRENCY_MISMATCH))
            continue
        settlement_id = leak.source_refs[0] if leak.source_refs else None
        if settlement_id is None or settlement_id not in covered:
            unobserved.append(Unobserved(leak.id, unit.arm, NOT_CARRIED_FORWARD))
            continue

        resolved = leak.id not in still_open
        observations.append(Observation(unit_id=leak.id, arm=unit.arm,
                                        amount=leak.amount, recovered=resolved))
        if unit.engine_claimed_recovered and not resolved:
            # The engine reported success and the bank data disagrees. This is
            # the check that keeps the recovery metric honest.
            claimed_not_observed.append(leak.id)

    return ObservationReport(observations=tuple(observations),
                             unobserved=tuple(unobserved),
                             claimed_not_observed=tuple(claimed_not_observed))


def measure_followup_lift(prior, followup, *, min_cohort: int = 30) -> Optional[CausalLift]:
    """Convenience: observe a follow-up batch and measure the lift in one call.

    ``None`` when the experiment cannot be measured — no observed units at all,
    or only one arm present. The caller must handle ``None``; there is no
    fallback number, because a fallback number is how a lift claim gets
    manufactured.
    """
    report = observe_followup(prior, followup)
    if not report.is_measurable():
        return None
    try:
        return report.measure(min_cohort=min_cohort)
    except MeasurementError:  # pragma: no cover - defensive: arms verified above
        return None
