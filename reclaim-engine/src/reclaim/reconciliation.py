"""Deterministic reconciliation core.

The first real RECLAIM capability, and deliberately **AI-free**: it matches
money across sources using *exact* rules only. Probabilistic / fuzzy matching
and the AI exception resolver are later layers that sit *above* this
deterministic gate (per the architecture's two-brain design). Nothing here
guesses.

Foundation scope — the canonical payment reconciliation:

    settlement payout  (gross - MDR - GST - TCS)   <->   bank credit

Rules (all exact, all deterministic — same input always yields same output,
goal **G4**):

- **Match** when a bank credit shares the settlement's UTR *and* its amount
  exactly equals the settlement's expected payout (``net_amount``).
- **Short payment** when the UTR matches but the bank credit is *less* than
  expected -> a recoverable leak for the shortfall.
- **Over credit** when the UTR matches but the bank credit is *more* than
  expected -> an (unexpected-fee) anomaly leak for the excess.
- **Missing settlement** when a settlement has no bank credit with its UTR ->
  a leak for the whole expected payout.
- **Unexpected credit** when a bank credit's UTR matches no settlement -> a
  timing/other-source leak for the whole amount.

Everything that does not reconcile becomes a typed :class:`LeakRecord` with a
plain-language hypothesis — the "honest exception list", never silently
dropped (goal **G2**).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .domain import LeakRecord, LeakType, Source, Transaction
from .money import Money

_RATE_Q = Decimal("0.0001")  # reconciliation rates reported to 4 dp


class ReconciliationError(Exception):
    """Raised on malformed reconciliation input (missing/duplicate UTRs, currency mix)."""


@dataclass(frozen=True)
class MatchPair:
    """An exact settlement <-> bank match."""

    settlement: Transaction
    bank: Transaction
    utr: str


@dataclass(frozen=True)
class ReconciliationResult:
    matched: tuple[MatchPair, ...]
    leaks: tuple[LeakRecord, ...]
    currency: str
    total_settlements: int
    total_expected: Money
    # Residuals with *no exact UTR match* on the other side — the candidates the
    # probabilistic layer (Phase 5) may still be able to pair. Short/over-payment
    # cases are excluded: they are UTR-matched, genuine discrepancies, not
    # re-matching candidates.
    unmatched_settlements: tuple[Transaction, ...] = ()
    unmatched_bank_credits: tuple[Transaction, ...] = ()

    # ---- counts / amounts --------------------------------------------
    @property
    def matched_count(self) -> int:
        return len(self.matched)

    @property
    def leak_count(self) -> int:
        return len(self.leaks)

    def matched_amount(self) -> Money:
        total = Money.zero(self.currency)
        for m in self.matched:
            total = total + m.bank.gross_amount
        return total

    def leaked_amount(self) -> Money:
        total = Money.zero(self.currency)
        for leak in self.leaks:
            total = total + leak.amount
        return total

    # ---- rates (Decimal, never float) --------------------------------
    def match_rate_by_count(self) -> Decimal:
        if self.total_settlements == 0:
            return Decimal("1").quantize(_RATE_Q)  # nothing to reconcile == reconciled
        return (Decimal(self.matched_count) / Decimal(self.total_settlements)).quantize(_RATE_Q)

    def match_rate_by_value(self) -> Decimal:
        if self.total_expected.is_zero:
            return Decimal("1").quantize(_RATE_Q)
        return (self.matched_amount().amount / self.total_expected.amount).quantize(_RATE_Q)


def _require_utr(txn: Transaction, label: str) -> str:
    utr = txn.refs.utr
    if not (isinstance(utr, str) and utr != ""):
        raise ReconciliationError(f"{label} {txn.id!r} is missing a UTR; cannot match deterministically")
    return utr


def _index_by_utr(txns, label: str) -> dict[str, Transaction]:
    index: dict[str, Transaction] = {}
    for t in txns:
        utr = _require_utr(t, label)
        if utr in index:
            raise ReconciliationError(f"duplicate UTR {utr!r} among {label}s; cannot match deterministically")
        index[utr] = t
    return index


def reconcile_settlements_to_bank(
    settlements: list[Transaction],
    bank_credits: list[Transaction],
) -> ReconciliationResult:
    """Reconcile settlement payouts against bank credits by exact UTR + amount."""
    all_txns = list(settlements) + list(bank_credits)
    if not all_txns:
        return ReconciliationResult((), (), "INR", 0, Money.zero("INR"))

    currencies = {t.currency for t in all_txns}
    if len(currencies) != 1:
        raise ReconciliationError(f"reconciliation must be single-currency, got {currencies}")
    currency = currencies.pop()

    for s in settlements:
        if s.source is not Source.SETTLEMENT:
            raise ReconciliationError(f"expected SETTLEMENT source, got {s.source} for {s.id!r}")
    for b in bank_credits:
        if b.source is not Source.BANK:
            raise ReconciliationError(f"expected BANK source, got {b.source} for {b.id!r}")

    bank_by_utr = _index_by_utr(bank_credits, "bank credit")
    _index_by_utr(settlements, "settlement")  # validates settlement UTRs unique/present
    settlement_utrs = {s.refs.utr for s in settlements}

    matched: list[MatchPair] = []
    leaks: list[LeakRecord] = []
    unmatched_settlements: list[Transaction] = []
    unmatched_bank_credits: list[Transaction] = []
    total_expected = Money.zero(currency)

    # Settlement side
    for s in settlements:
        utr = s.refs.utr
        expected = s.net_amount
        total_expected = total_expected + expected
        bank = bank_by_utr.get(utr)

        if bank is None:
            leaks.append(LeakRecord(
                id=f"leak:missing:{s.id}", amount=expected, leak_type=LeakType.MISSING_SETTLEMENT,
                source_refs=(s.id,),
                hypothesis="expected settlement payout not found among bank credits",
                recoverable=False, customer_ref=s.counterparty,
            ))
            unmatched_settlements.append(s)
            continue

        bank_amt = bank.gross_amount  # a bank credit has no fees, so gross == net == credited
        if bank_amt == expected:
            matched.append(MatchPair(settlement=s, bank=bank, utr=utr))
        elif expected > bank_amt:
            shortfall = expected - bank_amt
            leaks.append(LeakRecord(
                id=f"leak:short:{s.id}", amount=shortfall, leak_type=LeakType.SHORT_PAYMENT,
                source_refs=(s.id, bank.id),
                hypothesis=f"bank credit {bank_amt} is below expected payout {expected}",
                recoverable=True, customer_ref=s.counterparty,
            ))
        else:  # bank_amt > expected
            excess = bank_amt - expected
            leaks.append(LeakRecord(
                id=f"leak:over:{s.id}", amount=excess, leak_type=LeakType.UNEXPLAINED_FEE,
                source_refs=(s.id, bank.id),
                hypothesis=f"bank credit {bank_amt} exceeds expected payout {expected}",
                recoverable=False, customer_ref=s.counterparty,
            ))

    # Bank side — credits with no matching settlement
    for b in bank_credits:
        if b.refs.utr not in settlement_utrs:
            leaks.append(LeakRecord(
                id=f"leak:unexpected:{b.id}", amount=b.gross_amount, leak_type=LeakType.TIMING,
                source_refs=(b.id,),
                hypothesis="bank credit with no matching settlement (possible timing or other source)",
                recoverable=False, customer_ref=b.counterparty,
            ))
            unmatched_bank_credits.append(b)

    return ReconciliationResult(
        matched=tuple(matched),
        leaks=tuple(leaks),
        currency=currency,
        total_settlements=len(settlements),
        total_expected=total_expected,
        unmatched_settlements=tuple(unmatched_settlements),
        unmatched_bank_credits=tuple(unmatched_bank_credits),
    )
