"""Post-Sprint-3 tests — the medallion data platform (architecture §4).

The layer's whole reason to exist is that nothing gets dropped and nothing gets
guessed. So the two properties under most pressure here are `accounted_for()` —
every landed row lands in exactly one bucket — and the refusal to accept a
low-confidence reference, which is the one ingestion mistake that fails silently
rather than loudly.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from reclaim.domain import Source
from reclaim.ingest import (
    RULE_ADAPTER,
    RULE_DUPLICATE_UTR,
    RULE_LOW_CONFIDENCE,
    RULE_MISSING_UTR,
    BankNarrationAdapter,
    BronzeLayer,
    BronzeRecord,
    Extraction,
    IngestError,
    PgSettlementAdapter,
    RegexNarrationExtractor,
    normalise_utr,
    to_gold,
    to_silver,
)
from reclaim.money import Money

D = Decimal


def pg_row(sid="s1", gross="5000.00", utr="UTR000123456789", **kw):
    row = {"settlement_id": sid, "gross_amount": gross, "utr": utr,
           "settled_at": "2026-08-25T09:00:00", "merchant_id": "m-1"}
    row.update(kw)
    return row


def bank_row(tid="b1", amount="4882.00", narration="NEFT-UTR000123456789-ACME", **kw):
    row = {"txn_id": tid, "amount": amount, "value_date": "2026-08-25T10:00:00",
           "narration": narration, "direction": "credit"}
    row.update(kw)
    return row


def bronze(raw, *, source="file.csv", line=1, batch="b1"):
    layer = BronzeLayer()
    new, _dup = layer.land(batch, source, [raw])
    assert line == 1
    return new[0]


# --------------------------------------------------------------------------
# Bronze
# --------------------------------------------------------------------------
def test_bronze_record_validates_its_lineage():
    ok = {"batch_id": "b1", "source": "f.csv", "line_no": 1, "raw": {},
          "content_hash": "abc"}
    for field, bad in (("batch_id", ""), ("source", ""), ("content_hash", ""),
                       ("line_no", 0), ("line_no", True), ("raw", "not a dict")):
        with pytest.raises(IngestError):
            BronzeRecord(**{**ok, field: bad})


def test_lineage_names_the_exact_row():
    record = bronze(pg_row(), source="razorpay-aug.csv", batch="batch-7")
    assert record.lineage == "bronze:batch-7:razorpay-aug.csv:1"


def test_landing_validates_its_arguments():
    layer = BronzeLayer()
    with pytest.raises(IngestError):
        layer.land("", "f.csv", [])
    with pytest.raises(IngestError):
        layer.land("b1", "", [])
    with pytest.raises(IngestError):
        layer.land("b1", "f.csv", ["not a dict"])


def test_the_same_file_landed_twice_produces_no_new_records():
    """Content-addressed, so a re-delivery is recognised by its bytes rather
    than by its filename."""
    layer = BronzeLayer()
    rows = [pg_row("s1"), pg_row("s2", utr="UTR000123456790")]
    new, dup = layer.land("b1", "f.csv", rows)
    assert len(new) == 2 and dup == ()
    new2, dup2 = layer.land("b2", "f-redelivered.csv", rows)
    assert new2 == () and len(dup2) == 2
    assert layer.size == 2


def test_duplicates_are_returned_not_silently_discarded():
    layer = BronzeLayer()
    layer.land("b1", "f.csv", [pg_row()])
    _new, dup = layer.land("b1", "f.csv", [pg_row()])
    assert dup[0].raw == pg_row()          # the caller can report what was skipped


def test_bronze_keeps_the_row_exactly_as_received():
    weird = {"settlement_id": "s1", "extra_vendor_column": "  keep me  "}
    record = bronze(weird)
    assert record.raw == weird
    assert BronzeLayer().records() == ()


# --------------------------------------------------------------------------
# Reference normalisation — the bug this layer exists to catch
# --------------------------------------------------------------------------
def test_normalisation_makes_both_spellings_of_one_reference_agree():
    """A settlement writes UTR000123456789; a narration writes it after a label
    and separators. Unnormalised the two differ, the exact matcher finds nothing,
    and the batch reports 0% with no error anywhere."""
    assert normalise_utr("UTR000123456789") == "000123456789"
    assert normalise_utr("utr-000 123/456.789") == "000123456789"
    assert normalise_utr("000123456789") == "000123456789"


def test_normalisation_rejects_a_reference_that_is_not_alphanumeric():
    with pytest.raises(IngestError):
        normalise_utr("!!!")
    with pytest.raises(IngestError):
        normalise_utr(12345)


# --------------------------------------------------------------------------
# Narration extraction
# --------------------------------------------------------------------------
def test_a_labelled_reference_is_taken_at_full_confidence():
    found = RegexNarrationExtractor().extract("NEFT-UTR000123456789-ACME PAYMENTS")
    assert found.utr == "000123456789"
    assert found.confidence == D("1.0")
    assert "labelled" in found.basis


def test_a_bare_reference_after_a_scheme_prefix_is_believed_less():
    """The same shape appears in account numbers and internal ids, so it is not
    a certainty and does not claim to be."""
    found = RegexNarrationExtractor().extract("UPI/432198765432/PAYMENT")
    assert found.utr == "432198765432"
    assert found.confidence == D("0.75")
    assert "UPI" in found.basis


def test_an_unrecognised_narration_extracts_nothing():
    for narration in ("CASH DEPOSIT BRANCH 4471", "", "   ", "SALARY"):
        found = RegexNarrationExtractor().extract(narration)
        assert found.utr is None and found.confidence == D("0")


def test_a_non_string_narration_extracts_nothing():
    assert RegexNarrationExtractor().extract(None).utr is None


# --------------------------------------------------------------------------
# The settlement adapter — deductions unpacked at the boundary
# --------------------------------------------------------------------------
def test_fees_are_unpacked_so_nothing_downstream_sees_a_gross_payout():
    txn = PgSettlementAdapter().to_transaction(
        bronze(pg_row(gross="5000.00", mdr="100.00", gst_on_mdr="18.00")))
    assert txn.source is Source.SETTLEMENT
    assert txn.gross_amount == Money.of("5000.00", "INR")
    assert txn.fees.total() == Money.of("118.00", "INR")
    assert txn.net_amount == Money.of("4882.00", "INR")
    assert txn.counterparty == "m-1"


def test_absent_fee_columns_default_to_zero_not_to_missing():
    txn = PgSettlementAdapter().to_transaction(bronze(pg_row()))
    assert txn.fees.total() == Money.zero("INR")
    assert txn.net_amount == txn.gross_amount


def test_the_settlement_adapter_states_why_it_refused():
    cases = [
        (pg_row(sid=""), "'settlement_id' is required"),
        (pg_row(utr=""), "'utr' is required"),
        ({k: v for k, v in pg_row().items() if k != "gross_amount"},
         "'gross_amount' is required"),
        (pg_row(gross="not-a-number"), "is not an amount"),
        ({k: v for k, v in pg_row().items() if k != "settled_at"},
         "'settled_at' is required"),
        (pg_row(settled_at="25/08/2026"), "not an ISO-8601 timestamp"),
        (pg_row(gross="10.00", mdr="20.00"), "exceed gross"),
        (pg_row(mdr="oops"), "is not an amount"),
    ]
    for raw, expected in cases:
        with pytest.raises(IngestError) as exc:
            PgSettlementAdapter().to_transaction(bronze(raw))
        assert expected in str(exc.value), (expected, str(exc.value))


def test_an_order_id_is_carried_through_when_present():
    txn = PgSettlementAdapter().to_transaction(bronze(pg_row(order_id="ORD-9")))
    assert txn.refs.order_id == "ORD-9"


def test_a_reference_that_normalises_away_is_refused():
    with pytest.raises(IngestError):
        PgSettlementAdapter().to_transaction(bronze(pg_row(utr="UTR")))


# --------------------------------------------------------------------------
# The bank adapter
# --------------------------------------------------------------------------
def test_a_credit_with_a_labelled_reference_conforms():
    txn = BankNarrationAdapter().to_transaction(bronze(bank_row()))
    assert txn.source is Source.BANK
    assert txn.refs.utr == "000123456789"
    assert txn.match_confidence == D("1.0")
    assert txn.narration_raw == "NEFT-UTR000123456789-ACME"
    assert any("utr as written" in e for e in txn.evidence)


def test_a_debit_is_refused_because_treating_it_as_a_payout_invents_money():
    with pytest.raises(IngestError) as exc:
        BankNarrationAdapter().to_transaction(bronze(bank_row(direction="debit")))
    assert "would invent money" in str(exc.value)


def test_a_row_with_no_recognisable_reference_is_refused():
    with pytest.raises(IngestError) as exc:
        BankNarrationAdapter().to_transaction(
            bronze(bank_row(narration="CASH DEPOSIT BRANCH 4471")))
    assert "no recognised reference pattern" in str(exc.value)


def test_a_low_confidence_extraction_is_refused_rather_than_guessed():
    """The one ingestion mistake that does not fail loudly: a wrong reference
    silently creates a false match."""
    adapter = BankNarrationAdapter(min_confidence=D("0.90"))
    with pytest.raises(IngestError) as exc:
        adapter.to_transaction(bronze(bank_row(narration="UPI/432198765432/PAY")))
    assert "below 0.90" in str(exc.value)
    assert "silently creates a false match" in str(exc.value)


def test_lowering_the_threshold_accepts_the_same_row():
    adapter = BankNarrationAdapter(min_confidence=D("0.50"))
    txn = adapter.to_transaction(bronze(bank_row(narration="UPI/432198765432/PAY")))
    assert txn.refs.utr == "432198765432"


def test_the_bank_adapter_validates_its_threshold_and_fields():
    with pytest.raises(IngestError):
        BankNarrationAdapter(min_confidence=0.7)
    with pytest.raises(IngestError):
        BankNarrationAdapter().to_transaction(bronze(bank_row(txn_id="")))
    with pytest.raises(IngestError):
        BankNarrationAdapter().to_transaction(bronze(bank_row(amount="lots")))


def test_a_custom_extractor_drops_into_the_seam():
    class _Always:
        def extract(self, narration):
            return Extraction("ZZZ99999999", D("1.0"), "a stub that always knows")

    txn = BankNarrationAdapter(extractor=_Always()).to_transaction(
        bronze(bank_row(narration="anything at all")))
    assert txn.refs.utr == "ZZZ99999999"


def test_an_extractor_returning_an_unusable_reference_is_refused():
    class _Junk:
        def extract(self, narration):
            return Extraction("---", D("1.0"), "confidently wrong")

    with pytest.raises(IngestError):
        BankNarrationAdapter(extractor=_Junk()).to_transaction(bronze(bank_row()))


# --------------------------------------------------------------------------
# Silver — the invariant
# --------------------------------------------------------------------------
def test_every_landed_row_is_accounted_for():
    """G2 as arithmetic: a row cannot be silently dropped, because the counts
    would not add up."""
    layer = BronzeLayer()
    rows = [pg_row("s1"), pg_row(""), pg_row("s3", gross="bad", utr="UTR000123456791"),
            pg_row("s4", utr="UTR000123456792")]
    new, dup = layer.land("b1", "f.csv", rows)
    silver = to_silver(new, PgSettlementAdapter(), duplicates=dup)
    assert silver.landed == 4
    assert len(silver.transactions) == 2
    assert len(silver.quarantined) == 2
    assert silver.accounted_for() is True
    assert silver.summary()["accounted_for"] is True


def test_quarantine_is_totalled_by_cause():
    layer = BronzeLayer()
    new, _ = layer.land("b1", "f.csv", [
        bank_row("b1"),                                            # ok
        bank_row("b2", narration="CASH DEPOSIT 41"),                # no reference
        bank_row("b3", narration="UPI/432198765432/PAY"),           # low confidence
        bank_row("b4", direction="debit"),                          # adapter rule
    ])
    silver = to_silver(new, BankNarrationAdapter(min_confidence=D("0.90")))
    assert silver.quarantine_by_rule() == {RULE_ADAPTER: 1, RULE_LOW_CONFIDENCE: 1,
                                           RULE_MISSING_UTR: 1}
    assert silver.accounted_for() is True


def test_a_quarantined_row_keeps_its_line_number_and_reason():
    layer = BronzeLayer()
    new, _ = layer.land("b1", "hdfc-aug.csv", [bank_row("b1"), bank_row("b2", amount="x")])
    silver = to_silver(new, BankNarrationAdapter())
    held = silver.quarantined[0]
    assert held.record.line_no == 2
    assert held.summary()["source"] == "hdfc-aug.csv"
    assert "line 2" in held.reason


def test_an_empty_batch_is_still_accounted_for():
    silver = to_silver((), PgSettlementAdapter())
    assert silver.landed == 0 and silver.accounted_for() is True
    assert silver.quarantine_by_rule() == {}


# --------------------------------------------------------------------------
# Gold
# --------------------------------------------------------------------------
def _gold_from(pg_rows, bank_rows, **kw):
    layer = BronzeLayer()
    pg_new, pg_dup = layer.land("b1", "settlements.csv", pg_rows)
    bk_new, bk_dup = layer.land("b1", "statement.csv", bank_rows)
    lineage = {r.raw.get("settlement_id") or r.raw.get("txn_id"): r
               for r in layer.records()}
    return to_gold(to_silver(pg_new, PgSettlementAdapter(), duplicates=pg_dup),
                   to_silver(bk_new, BankNarrationAdapter(), duplicates=bk_dup),
                   bronze_by_txn=lineage, **kw)


def test_gold_splits_by_source_and_sorts_deterministically():
    gold = _gold_from([pg_row("s2", utr="UTR000123456790"), pg_row("s1")],
                      [bank_row("b1")])
    assert [t.id for t in gold.settlements] == ["s1", "s2"]
    assert [t.id for t in gold.bank_credits] == ["b1"]
    settlements, banks = gold.to_reconcile()
    assert len(settlements) == 2 and len(banks) == 1


def test_a_duplicate_utr_is_caught_at_the_boundary_not_inside_the_matcher():
    """Reconciliation raises on a duplicate UTR, losing the whole batch. Gold
    catches it while the line number is still known, and the rest still runs."""
    gold = _gold_from([pg_row("s1", utr="UTR000123456789"),
                       pg_row("s2", utr="UTR-000-123-456-789"),     # same after normalising
                       pg_row("s3", utr="UTR000123456790")],
                      [bank_row("b1")])
    assert [t.id for t in gold.settlements] == ["s3"]
    assert len(gold.quarantined) == 2
    assert {q.rule for q in gold.quarantined} == {RULE_DUPLICATE_UTR}
    assert "arrival order" in gold.quarantined[0].reason


def test_both_sides_of_a_collision_are_quarantined():
    """Keeping the first would be a silent choice about which row is real."""
    gold = _gold_from([pg_row("s1"), pg_row("s2")], [])   # identical UTRs
    assert gold.settlements == ()
    assert len(gold.quarantined) == 2


def test_a_bank_side_collision_is_caught_too():
    gold = _gold_from([], [bank_row("b1"), bank_row("b2")])
    assert gold.bank_credits == ()
    assert {q.rule for q in gold.quarantined} == {RULE_DUPLICATE_UTR}


def test_silver_quarantines_are_carried_into_gold():
    gold = _gold_from([pg_row("s1"), pg_row("")], [bank_row("b1")])
    assert len(gold.settlements) == 1
    assert any(q.rule == RULE_ADAPTER for q in gold.quarantined)
    assert gold.summary() == {"settlements": 1, "bank_credits": 1, "quarantined": 1}


def test_gold_works_without_a_lineage_map():
    """A collision still reports, with a placeholder record rather than a crash."""
    layer = BronzeLayer()
    new, _ = layer.land("b1", "f.csv", [pg_row("s1"), pg_row("s2")])
    gold = to_gold(to_silver(new, PgSettlementAdapter()))
    assert len(gold.quarantined) == 2
    assert gold.quarantined[0].record.source == "unknown"


# --------------------------------------------------------------------------
# End to end through the real pipeline
# --------------------------------------------------------------------------
def test_the_platform_feeds_reconciliation_and_the_numbers_tie_out():
    from reclaim.pipeline import run_reclaim

    gold = _gold_from(
        [pg_row("s1", gross="5000.00", mdr="100.00", gst_on_mdr="18.00"),
         pg_row("s2", gross="3000.00", utr="UTR000123456790")],
        [bank_row("b1", amount="4882.00", narration="NEFT-UTR000123456789-ACME"),
         bank_row("b2", amount="2800.00", narration="UPI/UTR000123456790/PAYMENT")])
    settlements, banks = gold.to_reconcile()
    report = run_reclaim(settlements, banks)
    assert report.matched_amount == Money.of("4882.00", "INR")
    assert report.leaked_residual() == Money.of("200.00", "INR")
    assert {l.leak_type.value for l in report.residual_leaks} == {"short_payment"}


def test_lineage_survives_all_the_way_to_a_leak():
    """A leak can be traced back to the file and line it came from."""
    from reclaim.pipeline import run_reclaim

    gold = _gold_from([pg_row("s1", gross="3000.00")],
                      [bank_row("b1", amount="2800.00")])
    settlements, banks = gold.to_reconcile()
    report = run_reclaim(settlements, banks)
    assert report.residual_leaks[0].customer_ref == "m-1"
    assert settlements[0].evidence[0] == "bronze:b1:settlements.csv:1"


def test_re_landing_a_file_changes_nothing_downstream():
    """Replayability: the same delivery twice produces the same Gold view."""
    layer = BronzeLayer()
    rows = [pg_row("s1"), pg_row("s2", utr="UTR000123456790")]
    first, _ = layer.land("b1", "f.csv", rows)
    second, dup = layer.land("b2", "f.csv", rows)
    assert second == () and len(dup) == 2
    gold = to_gold(to_silver(first, PgSettlementAdapter(), duplicates=dup))
    assert [t.id for t in gold.settlements] == ["s1", "s2"]
    assert gold.quarantined == ()
