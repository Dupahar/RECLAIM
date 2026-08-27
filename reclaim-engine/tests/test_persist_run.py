"""Phase 12 tests — persistence wired into the pipeline."""
from datetime import datetime
from decimal import Decimal

from reclaim.money import Money
from reclaim.domain import Source, Transaction, TransactionRefs
from reclaim.pipeline import persist_run, run_reclaim
from reclaim.verification import verify_stores
from reclaim.persistence import (
    AuditRepository,
    InMemoryStore,
    JsonlFileStore,
    LedgerRepository,
)
from reclaim.resolver import GatedResolver, StaticResolver
from reclaim.recovery import AlwaysSucceedsExecutor, RecoveryEngine

TS = datetime(2026, 8, 26, 9, 0, 0)


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def _t(tid, source, amount, **refs):
    return Transaction(id=tid, source=source, gross_amount=inr(amount), ts=TS,
                       refs=TransactionRefs(**refs))


def _run():
    settlements = [_t("s1", Source.SETTLEMENT, "1000.00", utr="U1"),
                   _t("s2", Source.SETTLEMENT, "500.00", utr="U2"),   # short -> recovered
                   _t("s3", Source.SETTLEMENT, "750.00", utr="U3")]   # missing -> residual
    banks = [_t("b1", Source.BANK, "1000.00", utr="U1"),
             _t("b2", Source.BANK, "400.00", utr="U2")]
    resolver = GatedResolver(StaticResolver(True, Decimal("0.9")), StaticResolver(True, Decimal("0.95")))
    engine = RecoveryEngine(AlwaysSucceedsExecutor())
    return run_reclaim(settlements, banks, resolver=resolver, recovery_engine=engine, base_time=TS)


def test_persist_run_roundtrips_ledger_and_audit():
    rep = _run()
    ledger_store, audit_store = InMemoryStore(), InMemoryStore()
    audit = persist_run(rep, TS, ledger_store, audit_store)

    # Reload from the stores in fresh repositories.
    reloaded_ledger = LedgerRepository(ledger_store).load()
    reloaded_audit = AuditRepository(audit_store).load()

    # Ledger balances survive identically.
    assert reloaded_ledger.is_globally_balanced("INR")
    for acct in rep.ledger.accounts():
        assert reloaded_ledger.balance(acct, "INR") == rep.ledger.balance(acct, "INR")
    # Audit root survives identically, proofs still verify.
    assert reloaded_audit.root() == audit.root()
    root = reloaded_audit.root()
    for i, e in enumerate(reloaded_audit.events()):
        assert reloaded_audit.verify_inclusion(e, i, reloaded_audit.inclusion_proof(i), root)


def test_persist_run_is_deterministic():
    rep = _run()
    a1, a2 = InMemoryStore(), InMemoryStore()
    b1, b2 = InMemoryStore(), InMemoryStore()
    r1 = persist_run(rep, TS, a1, b1).root()
    r2 = persist_run(rep, TS, a2, b2).root()
    assert r1 == r2
    assert a1.read() == a2.read()   # identical persisted records


def test_persist_run_writes_expected_counts():
    rep = _run()
    ledger_store, audit_store = InMemoryStore(), InMemoryStore()
    persist_run(rep, TS, ledger_store, audit_store)
    # one posting per reconciled settlement (s1 exact) + one per recovery (s2)
    assert len(ledger_store.read()) == len(rep.ledger.postings())
    # audit events cover matches + recoveries + residual leaks
    assert len(audit_store.read()) >= 1


def test_persist_run_twice_is_a_no_op(tmp_path):
    """A re-persist must not change the stored root — otherwise a legitimate
    double-run is indistinguishable from tampering on replay."""
    ledger_file, audit_file = tmp_path / "ledger.jsonl", tmp_path / "audit.jsonl"
    ls, aus = JsonlFileStore(ledger_file), JsonlFileStore(audit_file)
    report = _run()

    first = persist_run(report, TS, ls, aus)
    lines_after_one = (audit_file.read_text(encoding="utf-8").splitlines(),
                       ledger_file.read_text(encoding="utf-8").splitlines())

    second = persist_run(report, TS, JsonlFileStore(ledger_file), JsonlFileStore(audit_file))
    lines_after_two = (audit_file.read_text(encoding="utf-8").splitlines(),
                       ledger_file.read_text(encoding="utf-8").splitlines())

    assert lines_after_one == lines_after_two          # nothing appended twice
    assert first.root() == second.root()

    res = verify_stores(JsonlFileStore(ledger_file), JsonlFileStore(audit_file),
                        expect_root=first.root())
    assert res.ok and res.audit_root == first.root()
