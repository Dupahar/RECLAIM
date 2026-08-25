"""Phase 3 tests — double-entry ledger core."""
from datetime import datetime

import pytest

from reclaim.money import Money
from reclaim.domain import Direction, LedgerEntry
from reclaim.ledger import (
    DuplicatePostingError,
    Ledger,
    LedgerError,
    Posting,
    UnbalancedPostingError,
)

TS = datetime(2026, 8, 25, 9, 0, 0)


def inr(x: str) -> Money:
    return Money.of(x, "INR")


def entry(eid, txn, account, direction, amount, currency="INR"):
    return LedgerEntry(id=eid, txn_id=txn, account=account, direction=direction,
                       amount=Money.of(amount, currency), ts=TS)


def balanced_posting(pid="p1"):
    """DEBIT bank 488200 + DEBIT fee 11800 == CREDIT sales 500000."""
    return Posting(
        id=pid, ts=TS,
        entries=(
            entry("e1", pid, "bank_account", Direction.DEBIT, "488200.00"),
            entry("e2", pid, "fee_expense", Direction.DEBIT, "11800.00"),
            entry("e3", pid, "sales_clearing", Direction.CREDIT, "500000.00"),
        ),
    )


# --------------------------------------------------------------------------
# Posting construction & the balance invariant
# --------------------------------------------------------------------------
def test_balanced_posting_ok():
    p = balanced_posting()
    assert p.total == inr("500000.00")
    assert p.currency == "INR"


def test_unbalanced_posting_rejected():
    with pytest.raises(UnbalancedPostingError):
        Posting(id="p1", ts=TS, entries=(
            entry("e1", "p1", "a", Direction.DEBIT, "100.00"),
            entry("e2", "p1", "b", Direction.CREDIT, "99.00"),
        ))


def test_zero_amount_entries_impossible_at_domain_layer():
    # Zero postings are impossible by construction: LedgerEntry rejects
    # non-positive amounts at the domain layer, so we can never even build the
    # entries for a zero-total posting. (Balanced + positive entries => total > 0.)
    from reclaim.domain import DomainError
    with pytest.raises(DomainError):
        entry("e1", "p1", "a", Direction.DEBIT, "0.00")


def test_entries_must_be_a_tuple():
    with pytest.raises(LedgerError):
        Posting(id="p1", ts=TS, entries=[  # a list, not a tuple
            entry("e1", "p1", "a", Direction.DEBIT, "100.00"),
            entry("e2", "p1", "b", Direction.CREDIT, "100.00"),
        ])  # type: ignore[arg-type]


def test_entries_must_all_be_ledgerentry():
    with pytest.raises(LedgerError):
        Posting(id="p1", ts=TS, entries=(
            "not-an-entry",  # type: ignore[arg-type]
            entry("e2", "p1", "b", Direction.CREDIT, "100.00"),
        ))


def test_posting_needs_two_entries():
    with pytest.raises(LedgerError):
        Posting(id="p1", ts=TS, entries=(
            entry("e1", "p1", "a", Direction.DEBIT, "100.00"),
        ))


def test_posting_single_currency():
    with pytest.raises(LedgerError):
        Posting(id="p1", ts=TS, entries=(
            entry("e1", "p1", "a", Direction.DEBIT, "100.00", "INR"),
            entry("e2", "p1", "b", Direction.CREDIT, "100.00", "USD"),
        ))


def test_entry_txn_id_must_match_posting():
    with pytest.raises(LedgerError):
        Posting(id="p1", ts=TS, entries=(
            entry("e1", "WRONG", "a", Direction.DEBIT, "100.00"),
            entry("e2", "p1", "b", Direction.CREDIT, "100.00"),
        ))


def test_entry_ids_unique_within_posting():
    with pytest.raises(LedgerError):
        Posting(id="p1", ts=TS, entries=(
            entry("dup", "p1", "a", Direction.DEBIT, "100.00"),
            entry("dup", "p1", "b", Direction.CREDIT, "100.00"),
        ))


def test_posting_requires_id_and_ts():
    with pytest.raises(LedgerError):
        Posting(id="", ts=TS, entries=balanced_posting().entries)
    with pytest.raises(LedgerError):
        Posting(id="p1", ts="nope", entries=balanced_posting().entries)  # type: ignore[arg-type]


def test_posting_is_immutable():
    p = balanced_posting()
    with pytest.raises(Exception):
        p.id = "other"  # type: ignore[misc]


# --------------------------------------------------------------------------
# Ledger posting, idempotency
# --------------------------------------------------------------------------
def test_post_and_read_back():
    led = Ledger()
    p = balanced_posting()
    led.post(p)
    assert led.postings() == (p,)
    assert set(led.accounts()) == {"bank_account", "fee_expense", "sales_clearing"}


def test_idempotent_repost_same_content():
    led = Ledger()
    p = balanced_posting("p1")
    led.post(p)
    led.post(balanced_posting("p1"))  # identical content, same id
    assert len(led.postings()) == 1  # no double count
    assert led.balance("bank_account", "INR") == inr("488200.00")


def test_same_id_different_content_rejected():
    led = Ledger()
    led.post(balanced_posting("p1"))
    other = Posting(id="p1", ts=TS, entries=(
        entry("x1", "p1", "a", Direction.DEBIT, "1.00"),
        entry("x2", "p1", "b", Direction.CREDIT, "1.00"),
    ))
    with pytest.raises(DuplicatePostingError):
        led.post(other)


def test_post_requires_posting_type():
    led = Ledger()
    with pytest.raises(LedgerError):
        led.post("not a posting")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Balances & global invariant
# --------------------------------------------------------------------------
def test_balances_debit_normal():
    led = Ledger()
    led.post(balanced_posting())
    assert led.balance("bank_account", "INR") == inr("488200.00")   # net debit
    assert led.balance("fee_expense", "INR") == inr("11800.00")     # net debit
    assert led.balance("sales_clearing", "INR") == inr("-500000.00")  # net credit


def test_debit_and_credit_totals():
    led = Ledger()
    led.post(balanced_posting())
    assert led.debit_total("bank_account", "INR") == inr("488200.00")
    assert led.credit_total("bank_account", "INR") == inr("0")
    assert led.credit_total("sales_clearing", "INR") == inr("500000.00")


def test_global_balance_holds():
    led = Ledger()
    led.post(balanced_posting("p1"))
    led.post(Posting(id="p2", ts=TS, entries=(
        entry("f1", "p2", "cash", Direction.DEBIT, "250.00"),
        entry("f2", "p2", "revenue", Direction.CREDIT, "250.00"),
    )))
    assert led.is_globally_balanced("INR") is True


def test_multi_currency_balances_are_isolated():
    led = Ledger()
    led.post(balanced_posting("inr1"))
    led.post(Posting(id="usd1", ts=TS, entries=(
        entry("u1", "usd1", "bank_account", Direction.DEBIT, "100.00", "USD"),
        entry("u2", "usd1", "sales_clearing", Direction.CREDIT, "100.00", "USD"),
    )))
    assert led.balance("bank_account", "INR") == inr("488200.00")
    assert led.balance("bank_account", "USD") == Money.of("100.00", "USD")
    assert led.is_globally_balanced("INR") and led.is_globally_balanced("USD")


# --------------------------------------------------------------------------
# Determinism / replay
# --------------------------------------------------------------------------
def test_replay_is_deterministic():
    postings = [
        balanced_posting("p1"),
        Posting(id="p2", ts=TS, entries=(
            entry("g1", "p2", "cash", Direction.DEBIT, "999.00"),
            entry("g2", "p2", "revenue", Direction.CREDIT, "999.00"),
        )),
    ]
    a = Ledger(); a.post_many(postings)
    b = Ledger(); b.post_many(postings)
    for acct in a.accounts():
        assert a.balance(acct, "INR") == b.balance(acct, "INR")
    assert a.postings() == b.postings()
