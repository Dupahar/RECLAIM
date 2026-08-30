"""Runnable holdout experiment:  python -m reclaim.experiment

Runs a cohort through the full loop at T, then re-reconciles a **follow-up
batch** at T+1 and measures what the chasing was actually worth.

    detect -> hold out 30% -> recover the rest -> re-reconcile at T+1
      -> observe both arms -> lift = treated rate - control rate

WHAT IS REAL HERE AND WHAT IS NOT — read this before quoting any number.

**Real:** the holdout assignment, the reconciliation at both T and T+1, the
observation loop (outcomes read out of the follow-up batch, never out of the
engine's claim), the lift arithmetic, the counterfactual subtraction, the
underpowered flag, and the scorecard.

**Synthetic:** the two batches. The T+1 file is a fixture generated below by a
deterministic hash rule, so the outcome *rates* are authored, which means the
lift this command prints is a property of the fixture, not evidence about real
payments. RECLAIM has never measured lift on production data. What this command
proves is that the machinery to measure it exists, is wired end to end, and
reports honestly — including reporting ``None`` when it cannot measure, and
flagging recoveries the bank data does not confirm.

The fixture's authored rates are stated in ``FIXTURE_NOTE`` and printed with the
result, so the reader can see the input the arithmetic was fed.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from .domain import Source, Transaction, TransactionRefs
from .measurement import CausalLift, HoldoutPolicy
from .money import Money
from .observation import ObservationReport, observe_followup
from .pipeline import RunReport, run_reclaim
from .recovery import AlwaysNotifies, AlwaysSucceedsExecutor, RecoveryEngine
from .scorecard import Scorecard, build_scorecard

TS = datetime(2026, 8, 25, 9, 0, 0)
TS_T1 = datetime(2026, 8, 26, 9, 0, 0)

COHORT_SIZE = 200
GROSS = "3000.00"
SHORTFALL = "200.00"
CREDITED = "2800.00"

HOLDOUT = HoldoutPolicy(control_pct=30, salt="reclaim-demo-t1")

# Authored outcome rates for the T+1 fixture. These are the *inputs* to the
# measurement, not findings. A real deployment reads these from the bank file.
TREATED_RESOLVE_PCT = 65
CONTROL_RESOLVE_PCT = 40
FIXTURE_NOTE = (
    f"fixture: {TREATED_RESOLVE_PCT}% of treated and {CONTROL_RESOLVE_PCT}% of control units "
    "are authored to resolve by T+1, so the expected lift is "
    f"~{TREATED_RESOLVE_PCT - CONTROL_RESOLVE_PCT}pp by construction"
)


def _inr(x: str) -> Money:
    return Money.of(x, "INR")


def _sid(i: int) -> str:
    return f"s{i:03d}"


def _fixture_resolves(unit_id: str, pct: int) -> bool:
    """Deterministic stand-in for 'did the money turn up by T+1?'.

    A separate salt from the holdout's, so which arm a unit is in and whether
    the fixture resolves it are independent — a shared salt would correlate
    assignment with outcome and manufacture a lift out of nothing.
    """
    digest = hashlib.sha256(f"followup-fixture:{unit_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100 < pct


def build_cohort_batch(*, resolved_settlement_ids=frozenset(), ts: datetime = TS):
    """A cohort of short-paid settlements. Ids in ``resolved_...`` are paid in full."""
    settlements, banks = [], []
    for i in range(COHORT_SIZE):
        sid = _sid(i)
        settlements.append(Transaction(id=sid, source=Source.SETTLEMENT,
                                       gross_amount=_inr(GROSS), ts=ts,
                                       refs=TransactionRefs(utr=f"UTR-{sid}")))
        credited = GROSS if sid in resolved_settlement_ids else CREDITED
        banks.append(Transaction(id=f"b-{sid}", source=Source.BANK,
                                 gross_amount=_inr(credited), ts=ts,
                                 refs=TransactionRefs(utr=f"UTR-{sid}")))
    return settlements, banks


def run_prior() -> RunReport:
    """The run at T: detect, hold out 30%, chase the rest under a real notice."""
    settlements, banks = build_cohort_batch()
    engine = RecoveryEngine(AlwaysSucceedsExecutor(), notice_executor=AlwaysNotifies())
    return run_reclaim(settlements, banks, recovery_engine=engine,
                       base_time=TS, holdout=HOLDOUT)


def build_followup(prior: RunReport) -> RunReport:
    """The T+1 re-reconciliation over a fixture whose outcomes are authored."""
    control_ids = {leak.id for leak in prior.control_leaks}
    resolved: set[str] = set()
    for leak in prior.exact.leaks:
        if not leak.recoverable:
            continue
        pct = CONTROL_RESOLVE_PCT if leak.id in control_ids else TREATED_RESOLVE_PCT
        if _fixture_resolves(leak.id, pct):
            resolved.add(leak.source_refs[0])
    settlements, banks = build_cohort_batch(resolved_settlement_ids=resolved, ts=TS_T1)
    return run_reclaim(settlements, banks)


def run_experiment() -> tuple[RunReport, RunReport, ObservationReport,
                              Optional[CausalLift], Scorecard]:
    """The whole two-period experiment, deterministically."""
    prior = run_prior()
    later = build_followup(prior)
    observed = observe_followup(prior, later)
    lift = observed.measure(min_cohort=30) if observed.is_measurable() else None
    return prior, later, observed, lift, build_scorecard(prior, lift)


def main() -> None:  # pragma: no cover - console output
    _prior, _later, observed, lift, card = run_experiment()
    o, c = observed.summary(), card.summary()
    print("=" * 64)
    print("  RECLAIM — holdout experiment, T and T+1 (deterministic)")
    print("=" * 64)
    print(f"  cohort              : {COHORT_SIZE} short-paid settlements of {SHORTFALL} each")
    print(f"  holdout             : {HOLDOUT.control_pct}% control, salt {HOLDOUT.salt!r}")
    print(f"  treated / control   : {o['treated_observed']} / {o['control_observed']}")
    print(f"  unobserved          : {o['unobserved']}")
    print("-" * 64)
    print("  T+1 observation (outcomes read from the follow-up batch):")
    if lift is None:
        print("    lift              : NOT MEASURABLE — one arm had no observed units")
    else:
        t, ct = lift.treated, lift.control
        print(f"    treated recovered : {t.recovered_n}/{t.n}  (rate {t.rate()})")
        print(f"    control recovered : {ct.recovered_n}/{ct.n}  (rate {ct.rate()})")
        print(f"    lift              : {lift.lift_pp} pp")
        print(f"    underpowered      : {lift.underpowered}")
        print("-" * 64)
        print("  the three numbers, in descending order of honesty:")
        print(f"    claimed  (engine said RECOVERED)      : {c['gross_recovered']}")
        print(f"    observed (bank data at T+1, treated)  : {t.recovered_amount}")
        print(f"    causal   (net of the control cohort)  : {c['causal_recovered']}")
    print("-" * 64)
    print(f"  claimed but not observed : {len(observed.claimed_not_observed)}"
          "   <- engine said RECOVERED, bank data did not agree")
    print(f"  notice compliance        : {c['notice_compliance']}")
    print(f"  contacts per unit        : {c['contacts_per_unit']}")
    print("=" * 64)
    print(f"  {FIXTURE_NOTE}.")
    print("  The batches are synthetic; the holdout, observation and lift")
    print("  arithmetic are real. No lift has been measured on real payments.")


if __name__ == "__main__":  # pragma: no cover
    main()
