"""Orchestration pipeline — the full RECLAIM loop, end to end.

``run_reclaim`` composes every layer over one batch:

    exact reconcile (Phase 4)
      -> probabilistic on the residual (Phase 5)
      -> gated AI resolver on the review band (Phase 6, optional)
      -> bounded recovery on recoverable leaks (Phase 7, optional)
      -> post reconciled + recovered money to the double-entry ledger (Phase 3)

It returns a ``RunReport`` with the honest three numbers (match rate / matched
/ recovered) plus the residual exception list and the AI/recovery audit. The
pipeline is deterministic: the resolver and recovery executor are injected, and
timestamps are supplied, so a run is fully reproducible (goal **G4**).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Optional

from .audit import AuditEvent, MerkleAuditLog
from .domain import Direction, LeakRecord, LeakType, LedgerEntry, RecoveryState, Transaction
from .persistence import AuditRepository, EventStore, LedgerRepository
from .ledger import Ledger, Posting
from .money import Money
from .probabilistic import DEFAULT_CONFIG as PROB_DEFAULT, MatchConfig, ScoredMatch, probabilistic_match
from .reconciliation import ReconciliationResult, reconcile_settlements_to_bank
from .recovery import FailureReason, RecoveryEngine, RecoveryOutcome
from .resolver import Decision, GatedResolver, ResolutionOutcome

_Q = Decimal("0.0001")


class PipelineError(Exception):
    pass


def _default_reason(leak: LeakRecord) -> FailureReason:
    return (FailureReason.INSUFFICIENT_FUNDS
            if leak.leak_type is LeakType.SHORT_PAYMENT else FailureReason.UNKNOWN)


def _settlement_posting(s: Transaction) -> Posting:
    entries = [
        LedgerEntry(f"{s.id}:bank", s.id, "bank_account", Direction.DEBIT, s.net_amount, s.ts),
        LedgerEntry(f"{s.id}:sales", s.id, "sales_clearing", Direction.CREDIT, s.gross_amount, s.ts),
    ]
    if s.fees is not None and s.fees.total().is_positive:
        entries.insert(1, LedgerEntry(f"{s.id}:fee", s.id, "fee_expense", Direction.DEBIT,
                                      s.fees.total(), s.ts))
    return Posting(id=s.id, ts=s.ts, entries=tuple(entries))


def _recovery_posting(leak: LeakRecord, amount: Money, ts: datetime) -> Posting:
    pid = f"recovery:{leak.id}"
    return Posting(id=pid, ts=ts, entries=(
        LedgerEntry(f"{leak.id}:r-bank", pid, "bank_account", Direction.DEBIT, amount, ts),
        LedgerEntry(f"{leak.id}:r-recv", pid, "merchant_receivable", Direction.CREDIT, amount, ts),
    ))


@dataclass(frozen=True)
class RunReport:
    currency: str
    total_expected: Money
    matched_amount: Money
    recovered_amount: Money
    residual_leaks: tuple[LeakRecord, ...]
    auto_matched: tuple[ScoredMatch, ...]
    ai_outcomes: tuple[ResolutionOutcome, ...]
    pending_review: tuple[ScoredMatch, ...]
    recoveries: tuple[RecoveryOutcome, ...]
    exact: ReconciliationResult
    ledger: Ledger

    def leaked_residual(self) -> Money:
        total = Money.zero(self.currency)
        for leak in self.residual_leaks:
            total = total + leak.amount
        return total

    def match_rate(self) -> Decimal:
        if self.total_expected.is_zero:
            return Decimal("1").quantize(_Q)
        return (self.matched_amount.amount / self.total_expected.amount).quantize(_Q)

    @property
    def ai_confirmed_count(self) -> int:
        return sum(1 for o in self.ai_outcomes if o.decision is Decision.CONFIRMED_MATCH)

    @property
    def ai_escalated_count(self) -> int:
        return sum(1 for o in self.ai_outcomes if o.decision is Decision.ESCALATE_HUMAN)

    def summary(self) -> dict:
        return {
            "currency": self.currency,
            "total_expected": str(self.total_expected),
            "matched": str(self.matched_amount),
            "match_rate": str(self.match_rate()),
            "recovered": str(self.recovered_amount),
            "residual": str(self.leaked_residual()),
            "residual_leaks": len(self.residual_leaks),
            "auto_matched": len(self.auto_matched),
            "ai_confirmed": self.ai_confirmed_count,
            "ai_escalated": self.ai_escalated_count,
            "pending_review": len(self.pending_review),
            "recovered_count": sum(1 for r in self.recoveries
                                   if r.final_state is RecoveryState.RECOVERED),
        }


def build_audit_log(report: "RunReport", at: datetime) -> MerkleAuditLog:
    """Turn a run into a tamper-evident Merkle audit log of its decisions.

    Every decision — including the *non*-actions (escalations, residual leaks) —
    is recorded, in a fixed deterministic order, so the whole run can be proven
    unaltered via the Merkle root and per-event inclusion proofs.
    """
    log = MerkleAuditLog()
    for m in report.exact.matched:
        log.append(AuditEvent("exact_match", at,
                              {"settlement": m.settlement.id, "bank": m.bank.id,
                               "amount": str(m.bank.gross_amount)}))
    for m in report.auto_matched:
        log.append(AuditEvent("fuzzy_auto_match", at,
                              {"settlement": m.settlement.id, "bank": m.bank.id, "score": str(m.score)}))
    for o in report.ai_outcomes:
        log.append(AuditEvent(f"ai_{o.decision.value}", at,
                              {"settlement": o.candidate.settlement.id, "bank": o.candidate.bank.id,
                               "confidence": str(o.confidence)}))
    for r in report.recoveries:
        log.append(AuditEvent(f"recovery_{r.final_state.value}", at,
                              {"leak": r.leak.id,
                               "amount": str(r.recovered_amount) if r.recovered_amount is not None else "0"}))
    for leak in report.residual_leaks:
        log.append(AuditEvent("residual_leak", at,
                              {"leak": leak.id, "amount": str(leak.amount), "type": leak.leak_type.value}))
    return log


def persist_run(report: "RunReport", at: datetime,
                ledger_store: EventStore, audit_store: EventStore) -> MerkleAuditLog:
    """Persist a run: append the ledger postings and the audit events to the
    given (append-only) stores. Returns the audit log so callers can report its
    root. Reloading the stores reproduces identical balances and Merkle root."""
    ledger_repo = LedgerRepository(ledger_store)
    for posting in report.ledger.postings():
        ledger_repo.save_posting(posting)
    audit = build_audit_log(report, at)
    audit_repo = AuditRepository(audit_store)
    for event in audit.events():
        audit_repo.append_event(event)
    return audit


def run_reclaim(
    settlements: list[Transaction],
    banks: list[Transaction],
    *,
    resolver: Optional[GatedResolver] = None,
    recovery_engine: Optional[RecoveryEngine] = None,
    reason_for: Optional[Callable[[LeakRecord], FailureReason]] = None,
    base_time: Optional[datetime] = None,
    prob_config: MatchConfig = PROB_DEFAULT,
) -> RunReport:
    """Run the full detect -> recover -> book loop over a batch."""
    if recovery_engine is not None and base_time is None:
        raise PipelineError("base_time is required when a recovery_engine is provided")
    reason_for = reason_for or _default_reason

    # 1) Exact reconciliation.
    exact = reconcile_settlements_to_bank(settlements, banks)
    currency = exact.currency

    # 2) Probabilistic matching on the no-UTR-match residual.
    prob = probabilistic_match(list(exact.unmatched_settlements),
                               list(exact.unmatched_bank_credits), prob_config)

    # 3) Gated AI resolver on the review band (optional).
    ai_outcomes: list[ResolutionOutcome] = []
    ai_confirmed_pairs: list[ScoredMatch] = []
    pending_review: list[ScoredMatch] = []
    if resolver is not None:
        for cand in prob.review_candidates:
            out = resolver.resolve(cand)
            ai_outcomes.append(out)
            if out.decision is Decision.CONFIRMED_MATCH:
                ai_confirmed_pairs.append(cand)
            else:
                pending_review.append(cand)
    else:
        pending_review.extend(prob.review_candidates)

    # 4) Determine reconciled settlements (exact + probabilistic auto + AI-confirmed).
    reconciled_settlements: dict[str, Transaction] = {m.settlement.id: m.settlement for m in exact.matched}
    for m in prob.auto_matches:
        reconciled_settlements[m.settlement.id] = m.settlement
    for c in ai_confirmed_pairs:
        reconciled_settlements[c.settlement.id] = c.settlement

    resolved_settlement_ids = ({m.settlement.id for m in prob.auto_matches}
                               | {c.settlement.id for c in ai_confirmed_pairs})
    resolved_bank_ids = ({m.bank.id for m in prob.auto_matches}
                         | {c.bank.id for c in ai_confirmed_pairs})

    # 5) Bounded recovery on recoverable leaks (optional).
    recoveries: list[RecoveryOutcome] = []
    recovered_leak_ids: set[str] = set()
    if recovery_engine is not None:
        for leak in exact.leaks:
            if leak.recoverable:
                out = recovery_engine.recover(leak, reason_for(leak), base_time)
                recoveries.append(out)
                if out.final_state is RecoveryState.RECOVERED:
                    recovered_leak_ids.add(leak.id)

    # 6) Post reconciled settlements and recovered money to the ledger.
    ledger = Ledger()
    for s in reconciled_settlements.values():
        ledger.post(_settlement_posting(s))
    recovered_amount = Money.zero(currency)
    for out in recoveries:
        if out.final_state is RecoveryState.RECOVERED and out.recovered_amount is not None:
            ledger.post(_recovery_posting(out.leak, out.recovered_amount, base_time))
            recovered_amount = recovered_amount + out.recovered_amount

    # 7) Honest residual exception list — leaks not resolved by any path.
    residual: list[LeakRecord] = []
    for leak in exact.leaks:
        if leak.leak_type is LeakType.MISSING_SETTLEMENT and leak.source_refs[0] in resolved_settlement_ids:
            continue
        if leak.leak_type is LeakType.TIMING and leak.source_refs[0] in resolved_bank_ids:
            continue
        if leak.recoverable and leak.id in recovered_leak_ids:
            continue
        residual.append(leak)

    # 8) Matched amount = expected payout summed over reconciled settlements.
    matched_amount = Money.zero(currency)
    for s in reconciled_settlements.values():
        matched_amount = matched_amount + s.net_amount

    return RunReport(
        currency=currency,
        total_expected=exact.total_expected,
        matched_amount=matched_amount,
        recovered_amount=recovered_amount,
        residual_leaks=tuple(residual),
        auto_matched=prob.auto_matches,
        ai_outcomes=tuple(ai_outcomes),
        pending_review=tuple(pending_review),
        recoveries=tuple(recoveries),
        exact=exact,
        ledger=ledger,
    )
