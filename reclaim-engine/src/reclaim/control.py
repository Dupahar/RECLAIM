"""Layer 6 — the agentic control plane's durable half.

Architecture §8: *"HITL gates are typed states, not afterthoughts: any action
above a value threshold, any low-confidence match, and any novel exception
routes to a human and **waits**."* And: *"a checkpointer lets any decision
pause, persist, and resume."*

Until now escalation was an **outcome**, not a state. `ESCALATE_HUMAN` and
`HALTED` were terminal labels on a report that then exited the process. Nothing
could pause, persist, and be picked up tomorrow by a named person; the "human in
the loop" had no loop to be in.

This module supplies the missing state machine.

    open a typed gate  ->  AWAITING_HUMAN  ->  APPROVED / REJECTED / CANCELLED
         (checkpoint)         (durable)              (resumable)

Four commitments, each of which exists to stop a specific bad thing:

**A gate is decided exactly once.** Re-deciding raises. Two approvals of one
high-value debit is the failure mode that costs a merchant real money, so it is
prevented structurally rather than by a UI that hides the button.

**Approval authorises; it does not act.** Deciding a gate writes down that a
human said yes. Money moves only when ``resume`` runs the bounded, idempotent
saga behind it — the architecture's stance that *"an agent can never move money
by deciding to; it can only enqueue a bounded, gated, idempotent saga"* applies
to humans in exactly the same way. A test asserts that approving a gate posts
nothing.

**Append-only, versioned.** Like the money ledger and the Leak Ledger, a state
change appends a new version rather than editing one, so "who approved this, on
what evidence, and when?" is always answerable. Deciding is idempotent by
content: replaying the same decision does not create a second version.

**An unattended gate is not a clean run.** ``still_waiting`` is reported, not
swallowed. A run that ends with gates open is incomplete by definition, and the
resume outcome names every gate it could not act on.

What this is *not*: a workflow engine. There is no scheduler, no timer, no
retry-on-crash — Temporal and a LangGraph checkpointer own those in the target
architecture. This is the durable state and the transition rules they would
persist, which is the part that has to be correct whichever engine runs it.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Optional

from .domain import LeakRecord, RecoveryState
from .money import Money
from .probabilistic import ScoredMatch
from .resolver import Decision


class ControlError(Exception):
    """Raised on an invalid control-plane transition."""


class GateKind(str, Enum):
    """Why a human was asked. Typed, because the resume action differs by kind."""

    AI_ESCALATION = "ai_escalation"      # the gated resolver would not decide
    RECOVERY_HALT = "recovery_halt"      # recovery stopped mid-flight
    AFA_CEILING = "afa_ceiling"          # debit needs customer authentication
    VALUE_THRESHOLD = "value_threshold"  # action large enough to need sign-off


class GateState(str, Enum):
    AWAITING_HUMAN = "awaiting_human"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"              # superseded before anyone got to it


_SETTLED = (GateState.APPROVED, GateState.REJECTED, GateState.CANCELLED)


@dataclass(frozen=True)
class Gate:
    """One checkpoint where the workflow stopped and asked.

    ``subject_ref`` is the thing being decided: a leak id for a recovery gate, or
    ``"<settlement>:<bank>"`` for a match gate. It is what ``resume`` dispatches
    on, so it is required and must be non-empty.
    """

    id: str
    kind: GateKind
    subject_ref: str
    question: str
    opened_at: datetime
    amount: Optional[Money] = None
    evidence: tuple[str, ...] = ()
    state: GateState = GateState.AWAITING_HUMAN
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    rationale: str = ""
    audit_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not (isinstance(self.id, str) and self.id):
            raise ControlError("gate id is required")
        if not isinstance(self.kind, GateKind):
            raise ControlError("kind must be a GateKind")
        if not (isinstance(self.subject_ref, str) and self.subject_ref):
            raise ControlError("subject_ref is required")
        if not (isinstance(self.question, str) and self.question):
            raise ControlError("question is required")
        if not isinstance(self.opened_at, datetime):
            raise ControlError("opened_at must be a datetime")
        if not isinstance(self.state, GateState):
            raise ControlError("state must be a GateState")
        if self.amount is not None and not isinstance(self.amount, Money):
            raise ControlError("amount must be Money or None")
        if not isinstance(self.evidence, tuple) or not all(isinstance(e, str) for e in self.evidence):
            raise ControlError("evidence must be a tuple of strings")
        settled = self.state in _SETTLED
        if settled and not (isinstance(self.decided_by, str) and self.decided_by):
            raise ControlError(f"a {self.state.value} gate must name who decided it")
        if settled and not isinstance(self.decided_at, datetime):
            raise ControlError(f"a {self.state.value} gate must record when it was decided")
        if not settled and (self.decided_by is not None or self.decided_at is not None):
            raise ControlError("an undecided gate cannot carry a decision")

    @property
    def is_settled(self) -> bool:
        return self.state in _SETTLED

    @property
    def is_waiting(self) -> bool:
        return self.state is GateState.AWAITING_HUMAN

    def summary(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "subject": self.subject_ref,
            "state": self.state.value,
            "amount": str(self.amount) if self.amount is not None else None,
            "question": self.question,
            "decided_by": self.decided_by,
            "rationale": self.rationale,
        }


class ControlPlane:
    """Append-only, versioned store of HITL gates — the durable checkpointer.

    Mirrors ``LeakLedger`` deliberately: same versioning contract, same
    idempotency rule, so there is one way to reason about mutable state in this
    codebase rather than two.
    """

    def __init__(self) -> None:
        self._versions: dict[str, list[Gate]] = {}

    # ---- writes -------------------------------------------------------
    def open_gate(self, gate: Gate) -> Gate:
        """Record a new gate, or replay an identical one (a no-op).

        Re-opening an id with *different* content is refused: a gate's question
        and evidence are what the human is answering, and silently changing them
        under an open decision would invalidate the answer.
        """
        if not isinstance(gate, Gate):
            raise ControlError("open_gate() requires a Gate")
        versions = self._versions.setdefault(gate.id, [])
        if not versions:
            versions.append(gate)
            return gate
        if versions[0] == gate:
            return versions[-1]              # identical replay
        raise ControlError(f"gate {gate.id!r} is already open with different content")

    def decide(self, gate_id: str, state: GateState, *, actor: str,
               at: datetime, rationale: str = "") -> Gate:
        """Settle a gate. Appends a version; never edits the open one."""
        if state not in _SETTLED:
            raise ControlError(f"{state.value} is not a decision")
        current = self._require(gate_id)
        if current.is_settled:
            if (current.state is state and current.decided_by == actor
                    and current.decided_at == at and current.rationale == rationale):
                return current               # identical replay of the same decision
            raise ControlError(
                f"gate {gate_id!r} is already {current.state.value} "
                f"(decided by {current.decided_by!r}); a gate is decided once")
        decided = replace(current, state=state, decided_by=actor,
                          decided_at=at, rationale=rationale)
        self._versions[gate_id].append(decided)
        return decided

    def approve(self, gate_id: str, *, actor: str, at: datetime, rationale: str = "") -> Gate:
        return self.decide(gate_id, GateState.APPROVED, actor=actor, at=at, rationale=rationale)

    def reject(self, gate_id: str, *, actor: str, at: datetime, rationale: str = "") -> Gate:
        return self.decide(gate_id, GateState.REJECTED, actor=actor, at=at, rationale=rationale)

    def cancel(self, gate_id: str, *, actor: str, at: datetime, rationale: str = "") -> Gate:
        """Withdraw a question that no longer needs answering (e.g. a later
        match resolved the exception). Recorded, not deleted."""
        return self.decide(gate_id, GateState.CANCELLED, actor=actor, at=at, rationale=rationale)

    # ---- reads --------------------------------------------------------
    def _require(self, gate_id: str) -> Gate:
        current = self.current(gate_id)
        if current is None:
            raise ControlError(f"unknown gate id {gate_id!r}")
        return current

    def current(self, gate_id: str) -> Optional[Gate]:
        versions = self._versions.get(gate_id)
        return versions[-1] if versions else None

    def history(self, gate_id: str) -> tuple[Gate, ...]:
        """Every version, oldest first — the gate's own audit trail."""
        return tuple(self._versions.get(gate_id, ()))

    def gates(self) -> tuple[Gate, ...]:
        return tuple(v[-1] for v in self._versions.values())

    def awaiting(self) -> tuple[Gate, ...]:
        """The human queue: what somebody still has to answer."""
        return tuple(g for g in self.gates() if g.is_waiting)

    def settled(self) -> tuple[Gate, ...]:
        return tuple(g for g in self.gates() if g.is_settled)

    def by_state(self, state: GateState) -> tuple[Gate, ...]:
        return tuple(g for g in self.gates() if g.state is state)

    def by_kind(self, kind: GateKind) -> tuple[Gate, ...]:
        return tuple(g for g in self.gates() if g.kind is kind)

    @property
    def size(self) -> int:
        return len(self._versions)

    @property
    def version_count(self) -> int:
        return sum(len(v) for v in self._versions.values())

    def amount_awaiting(self, currency: str) -> Money:
        """Money parked behind unanswered questions — the cost of the queue."""
        total = Money.zero(currency)
        for gate in self.awaiting():
            if gate.amount is not None and gate.amount.currency == currency:
                total = total + gate.amount
        return total


# --------------------------------------------------------------------------
# Deriving gates from a run — "typed states, not afterthoughts"
# --------------------------------------------------------------------------
def _pair_ref(candidate: ScoredMatch) -> str:
    return f"{candidate.settlement.id}:{candidate.bank.id}"


def gates_for_run(report, at: datetime, *,
                  value_threshold: Optional[Money] = None) -> tuple[Gate, ...]:
    """Every question this run has to ask a human, derived mechanically.

    The point of deriving rather than hand-raising is that a gate cannot be
    forgotten: if the run escalated, halted, or wants to act on an amount above
    the sign-off threshold, the gate exists whether or not anyone remembered to
    create it.

    Order is deterministic (AI outcomes, then recoveries, then residual leaks),
    so the queue a human sees is stable across runs.
    """
    if not isinstance(at, datetime):
        raise ControlError("at must be a datetime")
    if value_threshold is not None and not isinstance(value_threshold, Money):
        raise ControlError("value_threshold must be Money or None")

    gates: list[Gate] = []

    for outcome in report.ai_outcomes:
        if outcome.decision is not Decision.ESCALATE_HUMAN:
            continue
        ref = _pair_ref(outcome.candidate)
        gates.append(Gate(
            id=f"gate:match:{ref}", kind=GateKind.AI_ESCALATION, subject_ref=ref,
            question=(f"Do settlement {outcome.candidate.settlement.id} and bank credit "
                      f"{outcome.candidate.bank.id} refer to the same payment?"),
            opened_at=at, amount=outcome.candidate.bank.gross_amount,
            evidence=(f"probabilistic score {outcome.candidate.score}",
                      f"resolver confidence {outcome.confidence}",
                      f"resolver rationale: {outcome.rationale}"),
        ))

    for rec in report.recoveries:
        if rec.final_state is not RecoveryState.HALTED:
            continue
        # The AFA ceiling and a mid-flight stop are different questions: one asks
        # "has the customer authenticated?", the other "should we keep going?".
        # Both arrive as HALTED, and the rationale is what distinguishes them.
        afa = "AFA-free ceiling" in rec.rationale
        kind = GateKind.AFA_CEILING if afa else GateKind.RECOVERY_HALT
        question = ("Confirm the customer authenticated this debit?" if afa
                    else "Authorise one further recovery attempt?")
        gates.append(Gate(
            id=f"gate:recovery:{rec.leak.id}", kind=kind, subject_ref=rec.leak.id,
            question=question, opened_at=at, amount=rec.leak.amount,
            evidence=(f"halted after {len(rec.attempts)} attempt(s)",
                      f"reason: {rec.rationale}"),
        ))

    if value_threshold is not None:
        halted_ids = {r.leak.id for r in report.recoveries
                      if r.final_state is RecoveryState.HALTED}
        for leak in report.residual_leaks:
            if not leak.recoverable or leak.id in halted_ids:
                continue
            if leak.amount.currency != value_threshold.currency:
                continue
            if leak.amount <= value_threshold:
                continue
            gates.append(Gate(
                id=f"gate:value:{leak.id}", kind=GateKind.VALUE_THRESHOLD,
                subject_ref=leak.id,
                question=(f"Sign off chasing {leak.amount}, above the "
                          f"{value_threshold} threshold?"),
                opened_at=at, amount=leak.amount,
                evidence=(f"leak type {leak.leak_type.value}", leak.hypothesis),
            ))

    return tuple(gates)


def open_gates_for_run(report, at: datetime, *, plane: Optional[ControlPlane] = None,
                       value_threshold: Optional[Money] = None) -> ControlPlane:
    """Derive a run's gates and check them into a control plane."""
    plane = plane if plane is not None else ControlPlane()
    for gate in gates_for_run(report, at, value_threshold=value_threshold):
        plane.open_gate(gate)
    return plane


# --------------------------------------------------------------------------
# Resuming — where an authorisation becomes a bounded action
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ResumeOutcome:
    """What a resume actually did, and what it refused to do.

    ``still_waiting`` is a first-class field rather than something a caller has
    to go looking for: a resume that silently ignored unanswered gates would let
    an incomplete run read as a finished one.
    """

    confirmed_pairs: tuple[str, ...] = ()      # subject refs a human matched
    rejected_pairs: tuple[str, ...] = ()
    authorised_retries: tuple[str, ...] = ()   # leak ids cleared for one attempt
    written_off: tuple[str, ...] = ()          # leak ids a human declined to chase
    recoveries: tuple = ()                     # RecoveryOutcome per authorised retry
    still_waiting: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()              # cancelled, or no action for the kind

    @property
    def acted_on(self) -> int:
        return (len(self.confirmed_pairs) + len(self.rejected_pairs)
                + len(self.authorised_retries) + len(self.written_off))

    @property
    def is_complete(self) -> bool:
        """True only when no gate is still waiting on a human."""
        return not self.still_waiting

    def summary(self) -> dict:
        return {
            "acted_on": self.acted_on,
            "confirmed_pairs": list(self.confirmed_pairs),
            "rejected_pairs": list(self.rejected_pairs),
            "authorised_retries": list(self.authorised_retries),
            "written_off": list(self.written_off),
            "recovered": sum(1 for r in self.recoveries
                             if r.final_state is RecoveryState.RECOVERED),
            "still_waiting": list(self.still_waiting),
            "skipped": list(self.skipped),
            "complete": self.is_complete,
        }


def resume(report, plane: ControlPlane, *, recovery_engine=None,
           base_time: Optional[datetime] = None,
           reason_for=None, leak_ledger=None) -> ResumeOutcome:
    """Apply settled gate decisions; report the ones still unanswered.

    An approved match gate confirms the pair. An approved recovery gate
    authorises **one** bounded attempt through the injected engine — the same
    notice, ceiling and idempotency rules as any other attempt, because a human
    saying yes changes who is accountable, not what is safe. A rejection writes
    the leak off with a name attached, which is a better outcome than a leak
    sitting in a queue forever.

    ``recovery_engine`` may be omitted, in which case retries are *authorised
    and recorded* but not executed. That is the honest split: the authorisation
    is durable state, the execution is an effect.
    """
    if recovery_engine is not None and base_time is None:
        raise ControlError("base_time is required when a recovery_engine is provided")

    from .recovery import FailureReason        # local: avoids an import cycle

    reason_for = reason_for or (lambda leak: FailureReason.INSUFFICIENT_FUNDS)
    leak_by_id: dict[str, LeakRecord] = {l.id: l for l in report.exact.leaks}

    confirmed, rejected, authorised, written_off = [], [], [], []
    recoveries, waiting, skipped = [], [], []

    for gate in plane.gates():
        if gate.is_waiting:
            waiting.append(gate.id)
            continue
        if gate.state is GateState.CANCELLED:
            skipped.append(gate.id)
            continue

        approved = gate.state is GateState.APPROVED
        if gate.kind is GateKind.AI_ESCALATION:
            (confirmed if approved else rejected).append(gate.subject_ref)
            continue

        # Recovery-shaped gates all resolve to "chase once more" or "stop".
        leak = leak_by_id.get(gate.subject_ref)
        if leak is None:
            skipped.append(gate.id)          # subject not in this run
            continue
        if not approved:
            written_off.append(leak.id)
            if leak_ledger is not None:
                leak_ledger.transition(leak.id, RecoveryState.NOT_RECOVERABLE)
            continue

        authorised.append(leak.id)
        if recovery_engine is not None:
            outcome = recovery_engine.recover(leak, reason_for(leak), base_time)
            recoveries.append(outcome)
            if leak_ledger is not None:
                leak_ledger.transition(leak.id, outcome.final_state)

    return ResumeOutcome(
        confirmed_pairs=tuple(confirmed), rejected_pairs=tuple(rejected),
        authorised_retries=tuple(authorised), written_off=tuple(written_off),
        recoveries=tuple(recoveries), still_waiting=tuple(waiting),
        skipped=tuple(skipped),
    )
