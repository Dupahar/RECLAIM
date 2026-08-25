"""Capstone — a full run produces a tamper-evident audit trail.

Runs a rich batch (exact match, fuzzy auto-match, AI-adjudicated, recovery, and
a residual leak), builds the Merkle audit log of every decision, and proves:
(a) each recorded decision has a verifiable inclusion proof under the root, and
(b) altering any recorded decision is cryptographically detectable.
"""
from datetime import datetime

from reclaim.money import Money
from reclaim.domain import Source, Transaction, TransactionRefs
from reclaim.pipeline import build_audit_log, run_reclaim
from reclaim.audit import AuditEvent
from reclaim.resolver import GatedResolver, StaticResolver
from reclaim.recovery import AlwaysSucceedsExecutor, RecoveryEngine
from decimal import Decimal

TS = datetime(2026, 8, 25, 9, 0, 0)


def _t(tid, source, amount, **refs):
    return Transaction(id=tid, source=source, gross_amount=Money.of(amount, "INR"),
                       ts=TS, refs=TransactionRefs(**refs))


def _rich_run():
    settlements = [
        _t("s1", Source.SETTLEMENT, "1000.00", utr="U1"),                 # exact
        _t("s2", Source.SETTLEMENT, "2000.00", utr="U2", order_id="O2"),  # fuzzy auto
        _t("s3", Source.SETTLEMENT, "3000.00", utr="U3"),                 # AI review
        _t("s4", Source.SETTLEMENT, "500.00", utr="U4"),                  # short -> recover
        _t("s5", Source.SETTLEMENT, "750.00", utr="U5"),                  # residual
    ]
    banks = [
        _t("b1", Source.BANK, "1000.00", utr="U1"),
        _t("b2", Source.BANK, "2000.00", utr="U2X", order_id="O2"),       # garbled UTR, shared order
        _t("b3", Source.BANK, "3000.00", utr="U3X"),                      # garbled UTR only -> review
        _t("b4", Source.BANK, "400.00", utr="U4"),                        # short by 100
    ]
    resolver = GatedResolver(StaticResolver(True, Decimal("0.9")), StaticResolver(True, Decimal("0.95")))
    engine = RecoveryEngine(AlwaysSucceedsExecutor())
    return run_reclaim(settlements, banks, resolver=resolver, recovery_engine=engine, base_time=TS)


def test_audit_log_covers_all_decision_kinds_and_verifies():
    rep = _rich_run()
    log = build_audit_log(rep, TS)
    kinds = {e.kind for e in log.events()}
    # exact, fuzzy, AI, recovery, residual all recorded
    assert "exact_match" in kinds
    assert "fuzzy_auto_match" in kinds
    assert any(k.startswith("ai_") for k in kinds)
    assert any(k.startswith("recovery_") for k in kinds)
    assert "residual_leak" in kinds

    root = log.root()
    for i, event in enumerate(log.events()):
        assert log.verify_inclusion(event, i, log.inclusion_proof(i), root) is True


def test_audit_log_detects_tampering():
    rep = _rich_run()
    log = build_audit_log(rep, TS)
    root = log.root()
    # take a real entry, present a tampered version of it at the same index
    idx = 0
    tampered = AuditEvent(log.events()[idx].kind, TS, {"settlement": "HACKED", "bank": "x", "amount": "0"})
    assert log.verify_inclusion(tampered, idx, log.inclusion_proof(idx), root) is False
