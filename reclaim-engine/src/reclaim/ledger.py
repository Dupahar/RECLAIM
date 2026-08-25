"""Double-entry ledger core.

The provable-integrity heart of RECLAIM. A ``Posting`` is the atomic unit: a
group of two or more :class:`~reclaim.domain.LedgerEntry` lines that **must
balance** (total debits == total credits, single currency). The ``Ledger`` is
an append-only, immutable store of postings that is:

- **Balanced by construction** — an unbalanced posting cannot be created.
- **Idempotent** — posting the same id twice is a no-op; the same id with
  different content is an error (goal **G5**).
- **Deterministic & replayable** — ledger state is a pure function of the
  ordered postings; rebuilding from the same postings yields identical
  balances (goal **G4**).
- **Immutable** — entries and postings are frozen; corrections are new
  postings, never edits (goal **G3**).

This is an in-memory reference implementation. Durable storage
(event-sourced / TigerBeetle-style) is a later phase; the invariants defined
here are the contract any storage layer must preserve.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .domain import Direction, LedgerEntry
from .money import Money


class LedgerError(Exception):
    """Base class for ledger errors."""


class UnbalancedPostingError(LedgerError):
    """Raised when a posting's debits do not equal its credits."""


class DuplicatePostingError(LedgerError):
    """Raised when a posting id is reused with different content."""


def _sum(entries, currency: str) -> Money:
    total = Money.zero(currency)
    for e in entries:
        total = total + e.amount
    return total


@dataclass(frozen=True)
class Posting:
    """A balanced group of ledger entries — the atomic journal entry."""

    id: str
    ts: datetime
    entries: tuple[LedgerEntry, ...]
    memo: str = ""

    def __post_init__(self) -> None:
        if not (isinstance(self.id, str) and self.id != ""):
            raise LedgerError("posting id is required")
        if not isinstance(self.ts, datetime):
            raise LedgerError("posting ts must be a datetime")
        if not isinstance(self.entries, tuple):
            raise LedgerError("entries must be a tuple")
        if len(self.entries) < 2:
            raise LedgerError("a posting needs at least two entries (double-entry)")
        if not all(isinstance(e, LedgerEntry) for e in self.entries):
            raise LedgerError("all entries must be LedgerEntry")

        ids = [e.id for e in self.entries]
        if len(set(ids)) != len(ids):
            raise LedgerError("entry ids within a posting must be unique")

        if any(e.txn_id != self.id for e in self.entries):
            raise LedgerError("every entry.txn_id must equal the posting id")

        currencies = {e.amount.currency for e in self.entries}
        if len(currencies) != 1:
            raise LedgerError(f"a posting must be single-currency, got {currencies}")
        currency = currencies.pop()

        debits = _sum([e for e in self.entries if e.direction is Direction.DEBIT], currency)
        credits = _sum([e for e in self.entries if e.direction is Direction.CREDIT], currency)
        if debits != credits:
            raise UnbalancedPostingError(
                f"posting {self.id!r} unbalanced: debits {debits} != credits {credits}"
            )
        # No zero-total guard is needed: LedgerEntry forbids non-positive amounts,
        # so a balanced posting (debits == credits) with >= 2 positive entries
        # necessarily has a total > 0. Zero postings are impossible by construction.

    @property
    def currency(self) -> str:
        return self.entries[0].amount.currency

    @property
    def total(self) -> Money:
        """The balanced magnitude (== total debits == total credits)."""
        return _sum([e for e in self.entries if e.direction is Direction.DEBIT], self.currency)


class Ledger:
    """An append-only, idempotent store of balanced postings (in-memory)."""

    def __init__(self) -> None:
        # insertion-ordered by id; dict preserves order in Python 3.7+
        self._postings: dict[str, Posting] = {}

    # ---- writes -------------------------------------------------------
    def post(self, posting: Posting) -> Posting:
        """Append a posting. Idempotent by id; balance already guaranteed."""
        if not isinstance(posting, Posting):
            raise LedgerError("post() requires a Posting")
        existing = self._postings.get(posting.id)
        if existing is not None:
            if existing == posting:
                return existing  # idempotent replay — no double count
            raise DuplicatePostingError(
                f"posting id {posting.id!r} already exists with different content"
            )
        self._postings[posting.id] = posting
        return posting

    def post_many(self, postings) -> None:
        for p in postings:
            self.post(p)

    # ---- reads --------------------------------------------------------
    def postings(self) -> tuple[Posting, ...]:
        return tuple(self._postings.values())

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(e for p in self._postings.values() for e in p.entries)

    def accounts(self) -> set[str]:
        return {e.account for e in self.entries()}

    def debit_total(self, account: str, currency: str) -> Money:
        return _sum(
            [e for e in self.entries()
             if e.account == account and e.direction is Direction.DEBIT
             and e.amount.currency == currency],
            currency,
        )

    def credit_total(self, account: str, currency: str) -> Money:
        return _sum(
            [e for e in self.entries()
             if e.account == account and e.direction is Direction.CREDIT
             and e.amount.currency == currency],
            currency,
        )

    def balance(self, account: str, currency: str) -> Money:
        """Debit-normal balance: debit_total - credit_total.

        Positive for a net-debit account (assets/expenses), negative for a
        net-credit account (liabilities/income). Callers interpret by account
        type; the ledger stays type-agnostic.
        """
        return self.debit_total(account, currency) - self.credit_total(account, currency)

    def is_globally_balanced(self, currency: str) -> bool:
        """Across the whole ledger for a currency, total debits == total credits."""
        all_entries = self.entries()
        debits = _sum(
            [e for e in all_entries if e.direction is Direction.DEBIT and e.amount.currency == currency],
            currency,
        )
        credits = _sum(
            [e for e in all_entries if e.direction is Direction.CREDIT and e.amount.currency == currency],
            currency,
        )
        return debits == credits
