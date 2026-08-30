"""Phase 25 tests — the durable agentic control plane (architecture §8).

The claims this module makes are all safety claims, so most of these tests are
attempts to break one: decide a gate twice, move money by approving, rehydrate
into a state the state machine forbids, or let an unanswered question read as a
finished run.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from reclaim.control import (
    ControlError,
    ControlPlane,
    Gate,
    GateKind,
    GateState,
    ResumeOutcome,
    gates_for_run,
    open_gates_for_run,
    resume,
)
from reclaim.domain import LeakRecord, LeakType, RecoveryState, Source, Transaction, TransactionRefs
from reclaim.leak_ledger import LeakLedger
from reclaim.ledger import Ledger
from reclaim.money import Money
from reclaim.pipeline import RunReport
from reclaim.probabilistic import ScoredMatch
from reclaim.reconciliation import ReconciliationResult
from reclaim.recovery import (
    AlwaysFailsExecutor,
    AlwaysSucceedsExecutor,
    Channel,
    FailureReason,
    RecoveryAttempt,
    RecoveryEngine,
    RecoveryOutcome,
)
from reclaim.resolver import Decision, ResolutionOutcome

TS = datetime(2026, 8, 31, 9, 0, 0)
LATER = datetime(2026, 8, 31, 17, 30, 0)
D = Decimal


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def gate(gid="g1", kind=GateKind.RECOVERY_HALT, subject="leak:short:s1", **kw) -> Gate:
    base = dict(id=gid, kind=kind, subject_ref=subject, question="Authorise?",
                opened_at=TS)
    base.update(kw)
    return Gate(**base)


def leak(sid="s1", amount="200.00", *, recoverable=True,
         leak_type=LeakType.SHORT_PAYMENT) -> LeakRecord:
    return LeakRecord(id=f"leak:short:{sid}", amount=inr(amount), leak_type=leak_type,
                      source_refs=(sid,), hypothesis="synthetic", recoverable=recoverable)


def candidate(sid="s3", bid="b3", score="0.70") -> ScoredMatch:
    s = Transaction(id=sid, source=Source.SETTLEMENT, gross_amount=inr("1500.00"), ts=TS,
                    refs=TransactionRefs(utr=f"UTR-{sid}"))
    b = Transaction(id=bid, source=Source.BANK, gross_amount=inr("1500.00"), ts=TS,
                    refs=TransactionRefs(utr=f"UTR-{bid}"))
    return ScoredMatch(settlement=s, bank=b, score=D(score), band="review")


def escalation(cand=None) -> ResolutionOutcome:
    return ResolutionOutcome(candidate=cand or candidate(), decision=Decision.ESCALATE_HUMAN,
                             confidence=D("0.55"), rationale="model would not commit")


def halted(l=None, *, rationale="executor error; halted for human", attempts=1) -> RecoveryOutcome:
    l = l or leak()
    log = tuple(RecoveryAttempt(i, Channel.UPI_RETRY, TS, f"{l.id}:attempt:{i}", "failed")
                for i in range(attempts))
    return RecoveryOutcome(leak=l, final_state=RecoveryState.HALTED, attempts=log,
                           recovered_amount=None, rationale=rationale, notice_at=TS)


def report(*, leaks=(), residual=(), recoveries=(), ai_outcomes=()) -> RunReport:
    exact = ReconciliationResult(matched=(), leaks=tuple(leaks), currency="INR",
                                 total_settlements=len(leaks),
                                 total_expected=inr("10000.00"))
    return RunReport(currency="INR", total_expected=inr("10000.00"),
                     matched_amount=inr("0"), recovered_amount=inr("0"),
                     residual_leaks=tuple(residual), auto_matched=(),
                     ai_outcomes=tuple(ai_outcomes), pending_review=(),
                     recoveries=tuple(recoveries), exact=exact, ledger=Ledger())


# --------------------------------------------------------------------------
# Gate — the typed state
# --------------------------------------------------------------------------
def test_gate_requires_its_identifying_fields():
    for bad in ({"id": ""}, {"kind": "recovery_halt"}, {"subject_ref": ""},
                {"question": ""}, {"opened_at": "2026-08-31"}, {"state": "approved"},
                {"amount": 200}, {"evidence": ["a"]}, {"evidence": (1,)}):
        with pytest.raises(ControlError):
            gate(**bad)


def test_a_settled_gate_must_name_who_decided_it_and_when():
    with pytest.raises(ControlError):
        gate(state=GateState.APPROVED, decided_at=LATER)          # no actor
    with pytest.raises(ControlError):
        gate(state=GateState.APPROVED, decided_by="ops@x")        # no timestamp
    ok = gate(state=GateState.APPROVED, decided_by="ops@x", decided_at=LATER)
    assert ok.is_settled and not ok.is_waiting


def test_an_undecided_gate_cannot_carry_a_decision():
    """Otherwise a gate could be recorded as answered by nobody."""
    with pytest.raises(ControlError):
        gate(decided_by="ops@x")
    with pytest.raises(ControlError):
        gate(decided_at=LATER)


def test_gate_summary_renders_with_and_without_an_amount():
    assert gate(amount=inr("200.00")).summary()["amount"] == "200.00 INR"
    assert gate().summary()["amount"] is None
    assert gate().summary()["state"] == "awaiting_human"


# --------------------------------------------------------------------------
# ControlPlane — the durable checkpointer
# --------------------------------------------------------------------------
def test_opening_and_reading_a_gate():
    plane = ControlPlane()
    plane.open_gate(gate())
    assert plane.size == 1 and plane.version_count == 1
    assert plane.current("g1").is_waiting
    assert plane.awaiting() == plane.gates()
    assert plane.settled() == ()


def test_open_gate_rejects_a_non_gate():
    with pytest.raises(ControlError):
        ControlPlane().open_gate("g1")


def test_reopening_an_identical_gate_is_a_no_op():
    plane = ControlPlane()
    plane.open_gate(gate())
    plane.open_gate(gate())
    assert plane.version_count == 1


def test_reopening_a_gate_with_different_content_is_refused():
    """The question and evidence are what the human is answering; changing them
    under an open decision would invalidate the answer."""
    plane = ControlPlane()
    plane.open_gate(gate())
    with pytest.raises(ControlError):
        plane.open_gate(gate(question="Something else entirely?"))


def test_deciding_appends_a_version_and_never_edits():
    plane = ControlPlane()
    plane.open_gate(gate())
    plane.approve("g1", actor="ops@x", at=LATER, rationale="customer confirmed")
    history = plane.history("g1")
    assert len(history) == 2
    assert history[0].state is GateState.AWAITING_HUMAN     # the original is intact
    assert history[1].state is GateState.APPROVED
    assert history[1].decided_by == "ops@x" and history[1].decided_at == LATER


def test_a_gate_is_decided_exactly_once():
    """Two approvals of one debit is the failure mode that costs real money."""
    plane = ControlPlane()
    plane.open_gate(gate())
    plane.approve("g1", actor="ops@x", at=LATER)
    with pytest.raises(ControlError) as exc:
        plane.approve("g1", actor="someone.else@x", at=LATER)
    assert "decided once" in str(exc.value)
    with pytest.raises(ControlError):
        plane.reject("g1", actor="ops@x", at=LATER)


def test_replaying_the_identical_decision_is_idempotent():
    """A retried write must not create a second approval."""
    plane = ControlPlane()
    plane.open_gate(gate())
    a = plane.approve("g1", actor="ops@x", at=LATER, rationale="same")
    b = plane.approve("g1", actor="ops@x", at=LATER, rationale="same")
    assert a == b and plane.version_count == 2


def test_awaiting_human_is_not_a_decision():
    plane = ControlPlane()
    plane.open_gate(gate())
    with pytest.raises(ControlError):
        plane.decide("g1", GateState.AWAITING_HUMAN, actor="ops@x", at=LATER)


def test_deciding_an_unknown_gate_raises():
    with pytest.raises(ControlError):
        ControlPlane().approve("nope", actor="ops@x", at=LATER)


def test_cancellation_is_recorded_not_deleted():
    plane = ControlPlane()
    plane.open_gate(gate())
    plane.cancel("g1", actor="system", at=LATER, rationale="superseded by a later match")
    assert plane.current("g1").state is GateState.CANCELLED
    assert len(plane.history("g1")) == 2
    assert plane.awaiting() == ()


def test_unknown_gate_reads_as_none_with_empty_history():
    plane = ControlPlane()
    assert plane.current("ghost") is None
    assert plane.history("ghost") == ()


def test_queue_views_partition_by_state_and_kind():
    plane = ControlPlane()
    plane.open_gate(gate("g1", kind=GateKind.RECOVERY_HALT))
    plane.open_gate(gate("g2", kind=GateKind.AI_ESCALATION, subject="s3:b3"))
    plane.reject("g2", actor="ops@x", at=LATER)
    assert [g.id for g in plane.awaiting()] == ["g1"]
    assert [g.id for g in plane.settled()] == ["g2"]
    assert [g.id for g in plane.by_state(GateState.REJECTED)] == ["g2"]
    assert [g.id for g in plane.by_kind(GateKind.RECOVERY_HALT)] == ["g1"]


def test_amount_awaiting_is_the_cost_of_the_queue():
    plane = ControlPlane()
    plane.open_gate(gate("g1", amount=inr("200.00")))
    plane.open_gate(gate("g2", amount=inr("750.00")))
    plane.open_gate(gate("g3", amount=None))                       # unpriced question
    plane.open_gate(gate("g4", amount=Money.of("99", "USD")))      # another currency
    plane.open_gate(gate("g5", amount=inr("1000.00")))
    plane.approve("g5", actor="ops@x", at=LATER)                   # answered: not parked
    assert plane.amount_awaiting("INR") == inr("950.00")


# --------------------------------------------------------------------------
# Deriving gates from a run — "not afterthoughts"
# --------------------------------------------------------------------------
def test_every_ai_escalation_becomes_a_gate():
    r = report(ai_outcomes=(escalation(),))
    gates = gates_for_run(r, TS)
    assert len(gates) == 1
    g = gates[0]
    assert g.kind is GateKind.AI_ESCALATION
    assert g.subject_ref == "s3:b3" and g.id == "gate:match:s3:b3"
    assert g.amount == inr("1500.00")
    assert any("probabilistic score" in e for e in g.evidence)


def test_a_decided_ai_outcome_raises_no_gate():
    confirmed = ResolutionOutcome(candidate=candidate(), decision=Decision.CONFIRMED_MATCH,
                                  confidence=D("0.95"), rationale="agreed")
    assert gates_for_run(report(ai_outcomes=(confirmed,)), TS) == ()


def test_a_halted_recovery_becomes_a_gate():
    l = leak()
    gates = gates_for_run(report(leaks=(l,), recoveries=(halted(l),)), TS)
    assert len(gates) == 1 and gates[0].kind is GateKind.RECOVERY_HALT
    assert gates[0].id == "gate:recovery:leak:short:s1"
    assert "further recovery attempt" in gates[0].question


def test_an_afa_halt_asks_a_different_question():
    """Both arrive as HALTED, but 'has the customer authenticated?' and 'should
    we keep going?' are not the same question and must not share a gate kind."""
    l = leak(amount="20000.00")
    r = report(leaks=(l,), recoveries=(halted(l, rationale=(
        "amount 20000.00 INR exceeds the AFA-free ceiling 15000.00 INR; "
        "a recurring debit this size needs customer authentication -- handed to a human"),
        attempts=0),))
    g = gates_for_run(r, TS)[0]
    assert g.kind is GateKind.AFA_CEILING
    assert "authenticated" in g.question


def test_a_successful_recovery_raises_no_gate():
    l = leak()
    ok = RecoveryOutcome(leak=l, final_state=RecoveryState.RECOVERED, attempts=(),
                         recovered_amount=l.amount, rationale="recovered", notice_at=TS)
    assert gates_for_run(report(leaks=(l,), recoveries=(ok,)), TS) == ()


def test_no_value_gates_without_a_threshold():
    big = leak(amount="50000.00")
    assert gates_for_run(report(leaks=(big,), residual=(big,)), TS) == ()


def test_a_high_value_leak_needs_sign_off():
    big, small = leak("s1", "50000.00"), leak("s2", "200.00")
    gates = gates_for_run(report(leaks=(big, small), residual=(big, small)), TS,
                          value_threshold=inr("15000.00"))
    assert [g.subject_ref for g in gates] == ["leak:short:s1"]
    assert gates[0].kind is GateKind.VALUE_THRESHOLD


def test_value_gates_skip_what_cannot_or_should_not_be_chased():
    unrecoverable = leak("s1", "50000.00", recoverable=False,
                         leak_type=LeakType.MISSING_SETTLEMENT)
    foreign = LeakRecord(id="leak:short:s2", amount=Money.of("50000", "USD"),
                         leak_type=LeakType.SHORT_PAYMENT, source_refs=("s2",),
                         hypothesis="x", recoverable=True)
    already_gated = leak("s3", "50000.00")
    r = report(leaks=(unrecoverable, foreign, already_gated),
               residual=(unrecoverable, foreign, already_gated),
               recoveries=(halted(already_gated),))
    gates = gates_for_run(r, TS, value_threshold=inr("15000.00"))
    # the halted one keeps its recovery gate; nothing gets a second, duplicate one
    assert [g.kind for g in gates] == [GateKind.RECOVERY_HALT]


def test_gate_derivation_validates_its_arguments():
    with pytest.raises(ControlError):
        gates_for_run(report(), "2026-08-31")
    with pytest.raises(ControlError):
        gates_for_run(report(), TS, value_threshold=15000)


def test_gate_order_is_deterministic():
    l = leak()
    r = report(leaks=(l,), residual=(l,), recoveries=(halted(l),),
               ai_outcomes=(escalation(),))
    once = [g.id for g in gates_for_run(r, TS)]
    assert once == [g.id for g in gates_for_run(r, TS)]
    assert once[0].startswith("gate:match:")        # AI outcomes first, then recoveries


def test_open_gates_for_run_checks_them_in():
    l = leak()
    r = report(leaks=(l,), recoveries=(halted(l),), ai_outcomes=(escalation(),))
    plane = open_gates_for_run(r, TS)
    assert plane.size == 2 and len(plane.awaiting()) == 2


def test_open_gates_for_run_can_extend_an_existing_plane():
    plane = ControlPlane()
    plane.open_gate(gate("pre-existing"))
    l = leak()
    open_gates_for_run(report(leaks=(l,), recoveries=(halted(l),)), TS, plane=plane)
    assert plane.size == 2


# --------------------------------------------------------------------------
# Resume — where an authorisation becomes a bounded action
# --------------------------------------------------------------------------
def test_approving_a_gate_moves_no_money():
    """'An agent can never move money by deciding to' applies to humans too.
    Approval is durable authorisation; the effect is a separate, bounded step."""
    l = leak()
    r = report(leaks=(l,), recoveries=(halted(l),))
    plane = open_gates_for_run(r, TS)
    plane.approve("gate:recovery:leak:short:s1", actor="ops@x", at=LATER)
    assert r.ledger.postings() == ()

    out = resume(r, plane)                       # no engine injected
    assert out.authorised_retries == ("leak:short:s1",)
    assert out.recoveries == ()                  # authorised, not executed
    assert r.ledger.postings() == ()


def test_resume_executes_the_bounded_saga_when_an_engine_is_given():
    l = leak()
    r = report(leaks=(l,), recoveries=(halted(l),))
    plane = open_gates_for_run(r, TS)
    plane.approve("gate:recovery:leak:short:s1", actor="ops@x", at=LATER)
    out = resume(r, plane, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                 base_time=LATER)
    assert len(out.recoveries) == 1
    assert out.recoveries[0].final_state is RecoveryState.RECOVERED
    assert out.summary()["recovered"] == 1


def test_an_authorised_retry_that_fails_is_reported_as_such():
    l = leak()
    r = report(leaks=(l,), recoveries=(halted(l),))
    plane = open_gates_for_run(r, TS)
    plane.approve("gate:recovery:leak:short:s1", actor="ops@x", at=LATER)
    out = resume(r, plane, recovery_engine=RecoveryEngine(AlwaysFailsExecutor()),
                 base_time=LATER)
    assert out.recoveries[0].final_state is RecoveryState.EXHAUSTED
    assert out.summary()["recovered"] == 0


def test_resume_requires_a_base_time_for_an_engine():
    with pytest.raises(ControlError):
        resume(report(), ControlPlane(), recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()))


def test_a_rejection_writes_the_leak_off_with_a_name_on_it():
    """Better than a leak sitting in a queue forever with nobody accountable."""
    l = leak()
    r = report(leaks=(l,), recoveries=(halted(l),))
    plane = open_gates_for_run(r, TS)
    plane.reject("gate:recovery:leak:short:s1", actor="ops@x", at=LATER,
                 rationale="customer disputed the mandate")
    ledger = LeakLedger()
    ledger.record(l)
    out = resume(r, plane, leak_ledger=ledger)
    assert out.written_off == ("leak:short:s1",)
    assert ledger.current(l.id).recovery_state is RecoveryState.NOT_RECOVERABLE
    assert ledger.open_queue() == ()


def test_an_authorised_retry_writes_its_outcome_back_to_the_leak_ledger():
    l = leak()
    r = report(leaks=(l,), recoveries=(halted(l),))
    plane = open_gates_for_run(r, TS)
    plane.approve("gate:recovery:leak:short:s1", actor="ops@x", at=LATER)
    ledger = LeakLedger()
    ledger.record(l)
    resume(r, plane, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
           base_time=LATER, leak_ledger=ledger)
    assert ledger.current(l.id).recovery_state is RecoveryState.RECOVERED
    assert len(ledger.history(l.id)) == 2


def test_an_approved_match_gate_confirms_the_pair():
    r = report(ai_outcomes=(escalation(),))
    plane = open_gates_for_run(r, TS)
    plane.approve("gate:match:s3:b3", actor="ops@x", at=LATER)
    out = resume(r, plane)
    assert out.confirmed_pairs == ("s3:b3",) and out.rejected_pairs == ()


def test_a_rejected_match_gate_leaves_the_pair_alone():
    r = report(ai_outcomes=(escalation(),))
    plane = open_gates_for_run(r, TS)
    plane.reject("gate:match:s3:b3", actor="ops@x", at=LATER)
    out = resume(r, plane)
    assert out.rejected_pairs == ("s3:b3",) and out.confirmed_pairs == ()


def test_an_unanswered_gate_makes_the_resume_incomplete():
    """A resume that silently ignored open gates would let an incomplete run
    read as a finished one."""
    l = leak()
    r = report(leaks=(l,), recoveries=(halted(l),), ai_outcomes=(escalation(),))
    plane = open_gates_for_run(r, TS)
    plane.approve("gate:match:s3:b3", actor="ops@x", at=LATER)
    out = resume(r, plane)
    assert out.still_waiting == ("gate:recovery:leak:short:s1",)
    assert out.is_complete is False
    assert out.summary()["complete"] is False


def test_a_cancelled_gate_is_skipped_not_acted_on():
    l = leak()
    r = report(leaks=(l,), recoveries=(halted(l),))
    plane = open_gates_for_run(r, TS)
    plane.cancel("gate:recovery:leak:short:s1", actor="system", at=LATER)
    out = resume(r, plane)
    assert out.skipped == ("gate:recovery:leak:short:s1",)
    assert out.acted_on == 0 and out.is_complete is True


def test_a_gate_whose_subject_left_the_run_is_skipped():
    r = report(leaks=(leak("s9"),))
    plane = ControlPlane()
    plane.open_gate(gate("g-orphan", subject="leak:short:gone"))
    plane.approve("g-orphan", actor="ops@x", at=LATER)
    out = resume(r, plane)
    assert out.skipped == ("g-orphan",) and out.authorised_retries == ()


def test_resume_honours_a_custom_failure_reason():
    l = leak()
    r = report(leaks=(l,), recoveries=(halted(l),))
    plane = open_gates_for_run(r, TS)
    plane.approve("gate:recovery:leak:short:s1", actor="ops@x", at=LATER)
    out = resume(r, plane, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                 base_time=LATER, reason_for=lambda leak: FailureReason.MANDATE_REVOKED)
    # a revoked mandate is permanent: the human's yes does not override the rules
    assert out.recoveries[0].final_state is RecoveryState.NOT_RECOVERABLE


def test_a_human_yes_does_not_bypass_the_afa_ceiling():
    """Approval changes who is accountable, not what is safe."""
    big = leak("s1", "50000.00")
    r = report(leaks=(big,), recoveries=(halted(big, rationale="AFA-free ceiling exceeded"),))
    plane = open_gates_for_run(r, TS)
    plane.approve("gate:recovery:leak:short:s1", actor="ops@x", at=LATER)
    out = resume(r, plane, recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()),
                 base_time=LATER)
    assert out.recoveries[0].final_state is RecoveryState.HALTED
    assert "AFA" in out.recoveries[0].rationale


def test_resume_outcome_summary_totals_the_work():
    out = ResumeOutcome(confirmed_pairs=("a:b",), rejected_pairs=("c:d",),
                        authorised_retries=("leak:1",), written_off=("leak:2",))
    assert out.acted_on == 4 and out.is_complete is True
    s = out.summary()
    assert s["acted_on"] == 4 and s["recovered"] == 0 and s["still_waiting"] == []


def test_an_empty_resume_is_complete_and_did_nothing():
    out = resume(report(), ControlPlane())
    assert out.acted_on == 0 and out.is_complete is True and out.summary()["skipped"] == []


def test_a_rejection_works_without_a_leak_ledger():
    """The Leak Ledger is optional; the decision is still recorded and reported."""
    l = leak()
    r = report(leaks=(l,), recoveries=(halted(l),))
    plane = open_gates_for_run(r, TS)
    plane.reject("gate:recovery:leak:short:s1", actor="ops@x", at=LATER)
    out = resume(r, plane)
    assert out.written_off == ("leak:short:s1",) and out.is_complete is True
