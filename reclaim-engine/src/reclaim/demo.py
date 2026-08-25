"""Runnable end-to-end demo:  python -m reclaim.demo

Builds a small synthetic batch and runs the full RECLAIM loop, printing the
honest report. Uses deterministic stand-ins for the two external integrations
(the LLM exception resolver and the payment executor), so the demo is fully
reproducible offline.
"""
from datetime import datetime
from decimal import Decimal

from .money import Money
from .domain import Fees, RecoveryState, Source, Transaction, TransactionRefs
from .pipeline import run_reclaim, RunReport
from .resolver import GatedResolver, StaticResolver
from .recovery import AlwaysSucceedsExecutor, RecoveryEngine

TS = datetime(2026, 8, 25, 9, 0, 0)


def _inr(x: str) -> Money:
    return Money.of(x, "INR")


def build_demo_batch():
    """A batch exercising every path: exact, fees, short-payment, garbled UTR, truly missing."""
    settlements = [
        # clean exact match, with fees (gross 5000 - 118 fees -> payout 4882)
        Transaction(id="s1", source=Source.SETTLEMENT, gross_amount=_inr("5000.00"), ts=TS,
                    fees=Fees(_inr("100.00"), _inr("18.00"), _inr("0"), _inr("0")),
                    refs=TransactionRefs(utr="UTR-1")),
        # short payment -> recover
        Transaction(id="s2", source=Source.SETTLEMENT, gross_amount=_inr("3000.00"), ts=TS,
                    refs=TransactionRefs(utr="UTR-2")),
        # garbled UTR but shared order id -> probabilistic auto-match
        Transaction(id="s3", source=Source.SETTLEMENT, gross_amount=_inr("1500.00"), ts=TS,
                    refs=TransactionRefs(utr="UTR-3", order_id="ORD-3")),
        # truly missing -> honest residual leak
        Transaction(id="s4", source=Source.SETTLEMENT, gross_amount=_inr("750.00"), ts=TS,
                    refs=TransactionRefs(utr="UTR-4")),
    ]
    banks = [
        Transaction(id="b1", source=Source.BANK, gross_amount=_inr("4882.00"), ts=TS,
                    refs=TransactionRefs(utr="UTR-1")),
        Transaction(id="b2", source=Source.BANK, gross_amount=_inr("2800.00"), ts=TS,
                    refs=TransactionRefs(utr="UTR-2")),   # short by 200
        Transaction(id="b3", source=Source.BANK, gross_amount=_inr("1500.00"), ts=TS,
                    refs=TransactionRefs(utr="UTR-3X", order_id="ORD-3")),  # garbled UTR
    ]
    return settlements, banks


def run_demo() -> RunReport:
    settlements, banks = build_demo_batch()
    resolver = GatedResolver(StaticResolver(True, Decimal("0.9")), StaticResolver(True, Decimal("0.95")))
    engine = RecoveryEngine(AlwaysSucceedsExecutor())
    return run_reclaim(settlements, banks, resolver=resolver, recovery_engine=engine, base_time=TS)


def main() -> None:  # pragma: no cover - console output
    rep = run_demo()
    s = rep.summary()
    print("=" * 56)
    print("  RECLAIM — end-to-end run (deterministic demo)")
    print("=" * 56)
    print(f"  total expected : {s['total_expected']}")
    print(f"  matched        : {s['matched']}   (rate {s['match_rate']})")
    print(f"  recovered      : {s['recovered']}")
    print(f"  residual       : {s['residual']}  ({s['residual_leaks']} leak/s)")
    print("-" * 56)
    print(f"  auto-matched (fuzzy) : {s['auto_matched']}")
    print(f"  AI confirmed         : {s['ai_confirmed']}")
    print(f"  AI escalated         : {s['ai_escalated']}")
    print(f"  recovered count      : {s['recovered_count']}")
    print("-" * 56)
    print("  honest residual exception list:")
    for leak in rep.residual_leaks:
        print(f"    - {leak.id}: {leak.amount} [{leak.leak_type.value}] {leak.hypothesis}")
    print(f"  ledger globally balanced (INR): {rep.ledger.is_globally_balanced('INR')}")
    print("=" * 56)
    print("  (LLM resolver and payment executor are deterministic stand-ins;")
    print("   real implementations drop in behind the same interfaces.)")


if __name__ == "__main__":  # pragma: no cover
    main()
