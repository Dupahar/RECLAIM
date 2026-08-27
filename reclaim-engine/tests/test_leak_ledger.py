"""Phase 19 tests — the Leak Ledger (architecture Layer 2, §5.2).

The seam between reconciliation (writer) and recovery (reader/writer), and the
object a human exception queue renders.
"""
from datetime import datetime

import pytest

from reclaim.money import Money
from reclaim.domain import LeakRecord, LeakType, RecoveryState
from reclaim.leak_ledger import LeakLedger, LeakLedgerError
from reclaim.pipeline import build_audit_log, build_leak_ledger
from reclaim.demo import run_demo, TS

D_TS = datetime(2026, 8, 25, 9, 0, 0)


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def leak(lid="L1", amount="100.00", ltype=LeakType.SHORT_PAYMENT, recoverable=True,
         state=RecoveryState.NONE, evidence=(), audit_ref=None):
    return LeakRecord(id=lid, amount=inr(amount), leak_type=ltype, source_refs=("s1",),
                      hypothesis="short", recoverable=recoverable, recovery_state=state,
                      evidence=evidence, audit_ref=audit_ref)


# --------------------------------------------------------------------------
# Append-only versioning
# --------------------------------------------------------------------------
def test_record_and_read_current():
    ll = LeakLedger()
    ll.record(leak())
    assert ll.size == 1 and ll.version_count == 1
    assert ll.current("L1").amount == inr("100.00")
    assert ll.current("missing") is None


def test_record_rejects_non_leak():
    with pytest.raises(LeakLedgerError):
        LeakLedger().record("not a leak")


def test_identical_replay_adds_no_version():
    ll = LeakLedger()
    ll.record(leak())
    ll.record(leak())
    assert ll.version_count == 1          # replaying a run cannot inflate the queue


def test_transition_appends_a_version_and_preserves_the_original():
    ll = LeakLedger()
    ll.record(leak())
    ll.transition("L1", RecoveryState.RECOVERED)
    assert ll.version_count == 2
    assert ll.current("L1").recovery_state is RecoveryState.RECOVERED
    # the original is still readable -- recovery never edits what recon wrote
    assert ll.history("L1")[0].recovery_state is RecoveryState.NONE
    assert [v.recovery_state.value for v in ll.history("L1")] == ["none", "recovered"]


def test_transition_rejects_unknown_leak_and_bad_state():
    ll = LeakLedger()
    ll.record(leak())
    with pytest.raises(LeakLedgerError):
        ll.transition("nope", RecoveryState.RECOVERED)
    with pytest.raises(LeakLedgerError):
        ll.transition("L1", "recovered")


def test_history_of_unknown_leak_is_empty():
    assert LeakLedger().history("nope") == ()


def test_record_many_and_ordering_is_deterministic():
    ll = LeakLedger()
    ll.record_many([leak("L3"), leak("L1"), leak("L2")])
    assert [l.id for l in ll.leaks()] == ["L3", "L1", "L2"]   # first-seen order


# --------------------------------------------------------------------------
# The classifier split and the human queue
# --------------------------------------------------------------------------
def test_recoverable_vs_accounting_exceptions():
    ll = LeakLedger()
    ll.record(leak("L1", recoverable=True))
    ll.record(leak("L2", recoverable=False))
    assert [l.id for l in ll.recoverable()] == ["L1"]
    assert [l.id for l in ll.accounting_exceptions()] == ["L2"]


def test_open_queue_excludes_everything_settled():
    ll = LeakLedger()
    ll.record(leak("recovered", state=RecoveryState.RECOVERED))
    ll.record(leak("dead", state=RecoveryState.NOT_RECOVERABLE))
    ll.record(leak("superseded", state=RecoveryState.SUPERSEDED))
    ll.record(leak("exhausted", state=RecoveryState.EXHAUSTED))
    ll.record(leak("halted", state=RecoveryState.HALTED))
    ll.record(leak("untouched", state=RecoveryState.NONE))
    assert sorted(l.id for l in ll.open_queue()) == ["exhausted", "halted", "untouched"]


def test_by_state_query():
    ll = LeakLedger()
    ll.record(leak("L1", state=RecoveryState.EXHAUSTED))
    ll.record(leak("L2", state=RecoveryState.NONE))
    assert [l.id for l in ll.by_state(RecoveryState.EXHAUSTED)] == ["L1"]


def test_totals_and_currencies_use_current_versions():
    ll = LeakLedger()
    ll.record(leak("L1", amount="100.00"))
    ll.record(leak("L2", amount="50.50"))
    usd = LeakRecord(id="L3", amount=Money.of("9.00", "USD"), leak_type=LeakType.TIMING)
    ll.record(usd)
    assert ll.total("INR") == inr("150.50")
    assert ll.total("USD") == Money.of("9.00", "USD")
    assert ll.currencies() == ("INR", "USD")


def test_total_of_empty_ledger_is_zero():
    assert LeakLedger().total("INR") == inr("0")


# --------------------------------------------------------------------------
# Built from a real run
# --------------------------------------------------------------------------
def test_build_leak_ledger_from_run_classifies_every_leak():
    report = run_demo()
    ll = build_leak_ledger(report)
    states = {l.id: l.recovery_state for l in ll.leaks()}
    assert states["leak:short:s2"] is RecoveryState.RECOVERED       # recovery ran
    assert states["leak:missing:s3"] is RecoveryState.SUPERSEDED    # fuzzy match won
    assert states["leak:missing:s4"] is RecoveryState.NONE          # genuinely open


def test_open_queue_equals_the_reports_honest_residual():
    """The queue a human sees must be exactly what the report calls residual."""
    report = run_demo()
    ll = build_leak_ledger(report)
    assert [l.id for l in ll.open_queue()] == [l.id for l in report.residual_leaks]


def test_audit_ref_resolves_to_a_valid_inclusion_proof():
    """audit_ref is '<root>:<leaf index>' — enough to prove the leak's own place."""
    report = run_demo()
    audit = build_audit_log(report, TS)
    ll = build_leak_ledger(report, audit)
    leak_rec = ll.current("leak:short:s2")
    root, index = leak_rec.audit_ref.rsplit(":", 1)
    index = int(index)
    assert root == audit.root()
    event = audit.events()[index]
    assert event.detail["leak"] == "leak:short:s2"
    assert audit.verify_inclusion(event, index, audit.inclusion_proof(index), root)


def test_build_without_audit_leaves_refs_unset():
    ll = build_leak_ledger(run_demo())
    assert all(l.audit_ref is None for l in ll.leaks())
