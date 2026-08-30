"""Learn-then-target, over two periods:  python -m reclaim.cycles

`python -m reclaim.experiment` proves the *measurement* works. This proves the
measurement is worth having, by using it:

    cycle 1: chase everyone      -> observe at T+1 -> fit an uplift model
    cycle 2: chase the persuadable -> observe at T+1 -> compare

The comparison is the product argument in one table. Cycle 1 is what a recovery
tool without uplift does. Cycle 2 is what the same engine does once it has one
period of observed outcomes to learn from.

**Why fewer contacts can recover more.** The cohort contains three behaviours,
and only one of them rewards contact:

| behaviour | shortfall | recovers if contacted | recovers if left alone |
|---|---|---|---|
| sure thing   | ₹200   | 90% | 85% |
| persuadable  | ₹1,500 | 65% | 25% |
| sleeping dog | ₹8,000 | 25% | 55% |

Chasing the sure things adds almost nothing. Chasing the sleeping dogs — and
they hold the largest amounts — makes the outcome *worse* than silence. A
response model ranks the sure things first, because 90% is the highest recovery
rate on the sheet. Uplift ranks the persuadable first, because +40pp is the only
number that represents money that would not otherwise have arrived.

**The holdout is measured against the whole policy, not the contacted subset.**
Both arms contain all three behaviours in both cycles: treated means "the policy
ran", which in cycle 2 legitimately includes units the policy chose not to
contact. That keeps the lift a statement about the deployed policy rather than
about a subset it selected, which is also why `run_reclaim` assigns cohorts
before targeting (ADR-0024).

WHAT IS REAL AND WHAT IS NOT.

**Real:** every mechanism. Reconciliation at both T and T+1, the holdout, the
observation loop reading outcomes from bank data rather than executor claims,
the uplift fit, the segment classification, the targeting decision inside
`run_reclaim`, the lift arithmetic, and the scorecard.

**Synthetic:** the batches, and the three behaviour profiles above are authored.
So the *size* of the improvement is a property of the fixture. What is not a
property of the fixture is the direction: given those behaviours, a targeting
policy that skips them is arithmetically better, and the code discovers which
ones to skip without being told.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from .domain import Source, Transaction, TransactionRefs
from .measurement import Arm, CausalLift, HoldoutPolicy
from .money import Money
from .observation import ObservationReport, observe_followup
from .pipeline import RunReport, Targeting, run_reclaim
from .recovery import (
    AlwaysNotifies,
    AlwaysSucceedsExecutor,
    FailureReason,
    RecoveryEngine,
)
from .scorecard import Scorecard, build_scorecard
from .uplift import Context, Segment, TrainingRow, UpliftModel
from .uplift import fit as fit_uplift

_Q = Decimal("0.0001")

COHORT_SIZE = 900          # divisible by 3, so the behaviours are balanced;
#                            large enough that every cell clears min_support in
#                            *both* arms, so the model reads its own cells rather
#                            than the pooled fallback (which, with a single failure
#                            reason, pools across behaviours and would mask them)
GROSS = "10000.00"
HOLDOUT = HoldoutPolicy(control_pct=30, salt="reclaim-cycles-holdout")

T0 = datetime(2026, 6, 1, 9, 0, 0)
T1 = datetime(2026, 7, 1, 9, 0, 0)
T2 = datetime(2026, 8, 1, 9, 0, 0)
T3 = datetime(2026, 9, 1, 9, 0, 0)


@dataclass(frozen=True)
class Behaviour:
    """One authored customer behaviour. The fixture's only inputs."""

    name: str
    shortfall: str
    contacted_pct: int
    untouched_pct: int

    @property
    def uplift_pp(self) -> int:
        return self.contacted_pct - self.untouched_pct


BEHAVIOURS = (
    Behaviour("sure thing", "200.00", contacted_pct=90, untouched_pct=85),
    Behaviour("persuadable", "1500.00", contacted_pct=65, untouched_pct=25),
    Behaviour("sleeping dog", "8000.00", contacted_pct=25, untouched_pct=55),
)


def _inr(x: str) -> Money:
    return Money.of(x, "INR")


def _sid(i: int) -> str:
    return f"s{i:04d}"


def behaviour_for(index: int) -> Behaviour:
    """Which behaviour a cohort member has. Round-robin, so the mix is exact."""
    return BEHAVIOURS[index % len(BEHAVIOURS)]


def _resolves(unit_id: str, pct: int, *, salt: str) -> bool:
    """Deterministic stand-in for 'did the money turn up by T+1?'.

    Salted separately from the holdout so cohort assignment and outcome are
    independent; salted per cycle so the two periods are independent draws
    rather than the same month twice.
    """
    digest = hashlib.sha256(f"{salt}:{unit_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100 < pct


def build_cohort(*, resolved_settlement_ids=frozenset(),
                 ts: datetime) -> tuple[list, list]:
    """A cohort of short-paid settlements, one per behaviour in rotation."""
    settlements, banks = [], []
    for i in range(COHORT_SIZE):
        sid, behaviour = _sid(i), behaviour_for(i)
        settlements.append(Transaction(id=sid, source=Source.SETTLEMENT,
                                       gross_amount=_inr(GROSS), ts=ts,
                                       refs=TransactionRefs(utr=f"UTR-{sid}")))
        credited = (_inr(GROSS) if sid in resolved_settlement_ids
                    else _inr(GROSS) - _inr(behaviour.shortfall))
        banks.append(Transaction(id=f"b-{sid}", source=Source.BANK,
                                 gross_amount=credited, ts=ts,
                                 refs=TransactionRefs(utr=f"UTR-{sid}")))
    return settlements, banks


def context_for(leak) -> Context:
    """The features targeting may use.

    Only the amount actually varies here, so the model's cells line up with the
    three behaviours — which is the point: a merchant can read the fitted table
    and see "we stop chasing the ₹8,000 shortfalls" rather than a coefficient.
    """
    return Context(failure_reason=FailureReason.INSUFFICIENT_FUNDS,
                   amount=leak.amount, days_since_failure=1, prior_failures=0)


def contacted_ids(report: RunReport) -> frozenset[str]:
    """Leaks the engine actually attempted. Not the same as 'treated': under a
    targeting policy a treated unit may deliberately never be contacted."""
    return frozenset(r.leak.id for r in report.recoveries if r.attempts)


def build_followup(prior: RunReport, *, salt: str, ts: datetime) -> RunReport:
    """The T+1 re-reconciliation. Outcome depends on *contact*, not on arm."""
    contacted = contacted_ids(prior)
    resolved: set[str] = set()
    for i in range(COHORT_SIZE):
        sid = _sid(i)
        leak_id = f"leak:short:{sid}"
        behaviour = behaviour_for(i)
        pct = (behaviour.contacted_pct if leak_id in contacted
               else behaviour.untouched_pct)
        if _resolves(leak_id, pct, salt=salt):
            resolved.add(sid)
    settlements, banks = build_cohort(resolved_settlement_ids=resolved, ts=ts)
    return run_reclaim(settlements, banks)


def training_rows(prior: RunReport, observed: ObservationReport) -> list[TrainingRow]:
    """Turn one cycle's observed outcomes into uplift training data.

    Contexts come from the *prior* run's leaks — the state that preceded the
    decision — and labels from the follow-up batch. Using the executor's claim
    as a label would teach the model that whatever the rail accepted is
    persuadable.
    """
    leak_by_id = {l.id: l for l in prior.exact.leaks}
    rows = []
    for obs in observed.observations:
        leak = leak_by_id.get(obs.unit_id)
        if leak is None:            # pragma: no cover - ids come from this run
            continue
        rows.append(TrainingRow(context=context_for(leak), arm=obs.arm,
                                recovered=obs.recovered))
    return rows


@dataclass(frozen=True)
class CycleResult:
    label: str
    prior: RunReport
    followup: RunReport
    observed: ObservationReport
    lift: Optional[CausalLift]
    card: Scorecard

    @property
    def contacts(self) -> int:
        return sum(len(r.attempts) for r in self.prior.recoveries)

    @property
    def units_contacted(self) -> int:
        return len(contacted_ids(self.prior))

    @property
    def causal(self) -> Optional[Money]:
        return self.card.causal_recovered

    def causal_per_contact(self) -> Optional[Decimal]:
        """Incremental rupees per contact — the number targeting is meant to move.

        ``None`` rather than zero when there is nothing to divide: no contacts,
        or no measurable lift to attribute.
        """
        if self.causal is None or self.contacts == 0:
            return None
        return (self.causal.amount / Decimal(self.contacts)).quantize(_Q)

    def summary(self) -> dict:
        return {
            "label": self.label,
            "units_contacted": self.units_contacted,
            "contacts": self.contacts,
            "skipped": len(self.prior.skipped_leaks),
            "skipped_value": str(self.prior.skipped_amount()),
            "lift_pp": str(self.lift.lift_pp) if self.lift else None,
            "claimed": str(self.card.gross_recovered),
            "observed": (str(self.lift.treated.recovered_amount) if self.lift else None),
            "causal": str(self.causal) if self.causal else None,
            "causal_per_contact": (str(self.causal_per_contact())
                                   if self.causal_per_contact() is not None else None),
        }


def run_cycle(label: str, *, targeting: Optional[Targeting], salt: str,
              t0: datetime, t1: datetime) -> CycleResult:
    """One full period: detect, hold out, target, chase, re-reconcile, measure."""
    settlements, banks = build_cohort(ts=t0)
    engine = RecoveryEngine(AlwaysSucceedsExecutor(), notice_executor=AlwaysNotifies())
    prior = run_reclaim(settlements, banks, recovery_engine=engine, base_time=t0,
                        holdout=HOLDOUT, targeting=targeting)
    followup = build_followup(prior, salt=salt, ts=t1)
    observed = observe_followup(prior, followup)
    lift = observed.measure(min_cohort=30) if observed.is_measurable() else None
    return CycleResult(label=label, prior=prior, followup=followup,
                       observed=observed, lift=lift,
                       card=build_scorecard(prior, lift))


def run_cycles() -> tuple[CycleResult, CycleResult, UpliftModel]:
    """Cycle 1 untargeted, then cycle 2 targeted by a model fitted on cycle 1."""
    first = run_cycle("cycle 1 - chase everyone", targeting=None,
                      salt="followup-cycle-1", t0=T0, t1=T1)
    model = fit_uplift(training_rows(first.prior, first.observed), min_support=40)
    targeting = Targeting(model=model, context_for=context_for)
    second = run_cycle("cycle 2 - chase the persuadable", targeting=targeting,
                       salt="followup-cycle-2", t0=T2, t1=T3)
    return first, second, model


def learned_table(model: UpliftModel) -> list[tuple[Behaviour, Segment, Optional[Decimal]]]:
    """What the model concluded about each authored behaviour, for display.

    This is the auditable artifact the discrete-cell choice buys: a merchant can
    read one row per behaviour instead of inspecting a fitted function.
    """
    out = []
    for behaviour in BEHAVIOURS:
        est = model.predict(Context(FailureReason.INSUFFICIENT_FUNDS,
                                    _inr(behaviour.shortfall), 1, 0))
        out.append((behaviour, est.segment, est.uplift))
    return out


def main() -> None:  # pragma: no cover - console output
    first, second, model = run_cycles()
    print("=" * 72)
    print("  RECLAIM - learn then target, over two periods (deterministic)")
    print("=" * 72)
    print(f"  cohort: {COHORT_SIZE} short-paid settlements, three authored behaviours")
    for b in BEHAVIOURS:
        print(f"    {b.name:<13} {b.shortfall:>9}  contacted {b.contacted_pct}% / "
              f"untouched {b.untouched_pct}%  ({b.uplift_pp:+d}pp)")
    print("-" * 72)
    print("  what cycle 1's observed outcomes taught the model:")
    for behaviour, segment, uplift in learned_table(model):
        verdict = "chase" if segment is Segment.PERSUADABLE else "skip"
        print(f"    {behaviour.shortfall:>9} shortfall -> {segment.value:<12} "
              f"uplift {uplift}  => {verdict}")
    print("-" * 72)
    a, b = first.summary(), second.summary()
    rows = [
        ("units contacted", "units_contacted"),
        ("contacts made", "contacts"),
        ("skipped by targeting", "skipped"),
        ("value skipped", "skipped_value"),
        ("lift (pp)", "lift_pp"),
        ("claimed recovered", "claimed"),
        ("observed recovered", "observed"),
        ("CAUSAL recovered", "causal"),
        ("causal per contact", "causal_per_contact"),
    ]
    print(f"  {'':<22}{'cycle 1':>20}{'cycle 2':>20}")
    print(f"  {'':<22}{'(chase everyone)':>20}{'(targeted)':>20}")
    for title, key in rows:
        print(f"  {title:<22}{str(a[key]):>20}{str(b[key]):>20}")
    print("=" * 72)
    print("  The batches and the three behaviour profiles are synthetic, so the")
    print("  size of the improvement is a property of the fixture. The direction")
    print("  is not: the model was told nothing about which behaviour is which,")
    print("  and found them from one period of observed bank outcomes.")


if __name__ == "__main__":  # pragma: no cover
    main()
