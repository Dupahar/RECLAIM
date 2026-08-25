"""Phase 13 tests — replay verification of a stored run."""
from datetime import datetime
from decimal import Decimal

from reclaim.money import Money
from reclaim.domain import Source, Transaction, TransactionRefs
from reclaim.pipeline import persist_run, run_reclaim
from reclaim.persistence import InMemoryStore
from reclaim.resolver import GatedResolver, StaticResolver
from reclaim.recovery import AlwaysSucceedsExecutor, RecoveryEngine
from reclaim.verification import verify_stores

TS = datetime(2026, 8, 26, 9, 0, 0)


def _t(tid, source, amount, **refs):
    return Transaction(id=tid, source=source, gross_amount=Money.of(amount, "INR"), ts=TS,
                       refs=TransactionRefs(**refs))


def _persisted():
    settlements = [_t("s1", Source.SETTLEMENT, "1000.00", utr="U1"),
                   _t("s2", Source.SETTLEMENT, "500.00", utr="U2"),
                   _t("s3", Source.SETTLEMENT, "750.00", utr="U3")]
    banks = [_t("b1", Source.BANK, "1000.00", utr="U1"),
             _t("b2", Source.BANK, "400.00", utr="U2")]
    rep = run_reclaim(settlements, banks,
                      resolver=GatedResolver(StaticResolver(True, Decimal("0.9"))),
                      recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()), base_time=TS)
    ledger_store, audit_store = InMemoryStore(), InMemoryStore()
    audit = persist_run(rep, TS, ledger_store, audit_store)
    return ledger_store, audit_store, audit.root()


def test_verify_good_run():
    ls, as_, root = _persisted()
    res = verify_stores(ls, as_)
    assert res.ok
    assert res.ledger_balanced
    assert res.proofs_ok
    assert res.audit_root == root
    assert res.root_matches_expected is None       # no expected root supplied
    assert res.currencies == ("INR",)


def test_verify_with_correct_expected_root():
    ls, as_, root = _persisted()
    res = verify_stores(ls, as_, expect_root=root)
    assert res.root_matches_expected is True and res.ok


def test_verify_detects_wrong_expected_root():
    ls, as_, _ = _persisted()
    res = verify_stores(ls, as_, expect_root="00" * 32)
    assert res.root_matches_expected is False
    assert res.ok is False                          # tamper/mismatch -> not ok


def test_verify_detects_tampered_audit_store():
    ls, as_, root = _persisted()
    # simulate tampering: append an extra (unexpected) audit record
    as_.append({"kind": "match", "at": TS.isoformat(), "detail": {"pair": "GHOST"}})
    res = verify_stores(ls, as_, expect_root=root)
    assert res.audit_root != root                   # root changed
    assert res.root_matches_expected is False and res.ok is False


def test_verify_empty_stores_is_vacuously_ok():
    res = verify_stores(InMemoryStore(), InMemoryStore())
    assert res.ok and res.ledger_balanced and res.proofs_ok
    assert res.posting_count == 0 and res.audit_events == 0
