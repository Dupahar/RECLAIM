"""The scorecard — architecture §9.3, "ungameable by design".

    match rate -> ₹ causally recovered (vs control) -> residual exception list
    -> false-positive / harassment rate -> time-to-closure

The design goal is that no single number can be improved by gaming it without
another one visibly getting worse. Raise the match rate by matching loosely and
the false-positive proxy moves. Recover more by chasing harder and the contact
rate and harassment checks move. Claim recovery without a control group and the
causal figure reports ``None`` rather than flattering you.

Every metric here is computed from a real ``RunReport``. Where a metric cannot
be computed from the data available, it is ``None`` with a stated reason — never
a zero standing in for "we didn't measure this".
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .domain import RecoveryState
from .measurement import CausalLift
from .money import Money

_Q = Decimal("0.0001")


@dataclass(frozen=True)
class Scorecard:
    currency: str
    # 1. detection and closure
    match_rate: Decimal
    closure_rate: Decimal
    total_expected: Money
    # 2. recovery, gross and causal
    gross_recovered: Money
    causal_recovered: Optional[Money]
    lift_pp: Optional[Decimal]
    causal_note: str
    # 3. the honest residual
    residual_amount: Money
    residual_count: int
    open_queue_count: int
    # 4. conduct
    units_contacted: int
    total_attempts: int
    contacts_per_unit: Optional[Decimal]
    wasted_contact_rate: Optional[Decimal]   # contacted, then found not recoverable
    notice_compliance: Optional[Decimal]     # share of debits preceded by a real notice
    halted_for_human: int
    # 5. speed
    time_to_closure_hours: Optional[Decimal]

    def summary(self) -> dict:
        def s(v):
            return None if v is None else str(v)
        return {
            "currency": self.currency,
            "match_rate": str(self.match_rate),
            "closure_rate": str(self.closure_rate),
            "total_expected": str(self.total_expected),
            "gross_recovered": str(self.gross_recovered),
            "causal_recovered": s(self.causal_recovered),
            "lift_pp": s(self.lift_pp),
            "causal_note": self.causal_note,
            "residual": str(self.residual_amount),
            "residual_count": self.residual_count,
            "open_queue_count": self.open_queue_count,
            "units_contacted": self.units_contacted,
            "total_attempts": self.total_attempts,
            "contacts_per_unit": s(self.contacts_per_unit),
            "wasted_contact_rate": s(self.wasted_contact_rate),
            "notice_compliance": s(self.notice_compliance),
            "halted_for_human": self.halted_for_human,
            "time_to_closure_hours": s(self.time_to_closure_hours),
        }


def build_scorecard(report, lift: Optional[CausalLift] = None) -> Scorecard:
    """Build the five-part scorecard from a run, and a causal lift if measured."""
    recoveries = report.recoveries

    units_contacted = sum(1 for r in recoveries if r.attempts)
    total_attempts = sum(len(r.attempts) for r in recoveries)
    contacts_per_unit = (Decimal(total_attempts) / Decimal(units_contacted)).quantize(_Q) \
        if units_contacted else None

    # Conduct: a unit we contacted and then classified as not recoverable was
    # contacted for nothing. Chasing harder pushes this up, which is the point.
    wasted = sum(1 for r in recoveries
                 if r.attempts and r.final_state is RecoveryState.NOT_RECOVERABLE)
    wasted_contact_rate = (Decimal(wasted) / Decimal(units_contacted)).quantize(_Q) \
        if units_contacted else None

    # Compliance: what share of contacted units had a notice actually dispatched?
    # Without a NoticeExecutor this is 0 — the window was modelled, not served.
    notified = sum(1 for r in recoveries if r.attempts and r.notice_sent)
    notice_compliance = (Decimal(notified) / Decimal(units_contacted)).quantize(_Q) \
        if units_contacted else None

    halted = sum(1 for r in recoveries if r.final_state is RecoveryState.HALTED)

    # Speed: hours from the notice to the last attempt across the batch.
    stamps = [a.at_time for r in recoveries for a in r.attempts]
    starts = [r.notice_at for r in recoveries if r.notice_at is not None]
    if stamps and starts:
        span = max(stamps) - min(starts)
        ttc = (Decimal(span.total_seconds()) / Decimal(3600)).quantize(_Q)
    else:
        ttc = None

    if lift is not None and lift.is_measurable():
        causal_recovered, lift_pp, note = lift.incremental_amount, lift.lift_pp, lift.note
    elif lift is not None:
        causal_recovered, lift_pp, note = None, None, lift.note
    else:
        causal_recovered, lift_pp = None, None
        note = ("no holdout was run, so recovery cannot be credited causally; "
                "gross_recovered includes money that may have arrived anyway")

    return Scorecard(
        currency=report.currency,
        match_rate=report.match_rate(),
        closure_rate=report.closure_rate(),
        total_expected=report.total_expected,
        gross_recovered=report.recovered_amount,
        causal_recovered=causal_recovered,
        lift_pp=lift_pp,
        causal_note=note,
        residual_amount=report.leaked_residual(),
        residual_count=len(report.residual_leaks),
        open_queue_count=len(report.residual_leaks) + len(report.control_leaks),
        units_contacted=units_contacted,
        total_attempts=total_attempts,
        contacts_per_unit=contacts_per_unit,
        wasted_contact_rate=wasted_contact_rate,
        notice_compliance=notice_compliance,
        halted_for_human=halted,
        time_to_closure_hours=ttc,
    )
