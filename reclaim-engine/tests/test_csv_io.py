"""Phase 15 tests — CSV input adapter."""
import pathlib

import pytest

from reclaim.money import Money
from reclaim.domain import Source
from reclaim.batch_io import BatchLoadError, load_batch_file
from reclaim.csv_io import load_batch_csv

ROOT = pathlib.Path(__file__).resolve().parents[1]
HEADER = ("record_type,id,gross_amount,currency,ts,"
          "mdr,gst_on_mdr,tcs,other,utr,rrn,order_id,invoice_no")


def _write(tmp_path, *rows):
    p = tmp_path / "b.csv"
    p.write_text("\n".join((HEADER,) + rows) + "\n", encoding="utf-8")
    return p


def test_shipped_csv_loads():
    settlements, banks = load_batch_csv(ROOT / "examples" / "sample_batch.csv")
    assert len(settlements) == 4 and len(banks) == 3
    assert settlements[0].fees.total() == Money.of("118.00", "INR")   # mdr 100 + gst 18
    assert settlements[0].refs.order_id == "ORD-1"
    assert banks[0].source is Source.BANK


def test_csv_matches_json_equivalent():
    # The shipped CSV and JSON samples describe the same batch -> identical objects.
    s_csv, b_csv = load_batch_csv(ROOT / "examples" / "sample_batch.csv")
    s_json, b_json = load_batch_file(ROOT / "examples" / "sample_batch.json")
    assert s_csv == s_json and b_csv == b_json


def test_blank_fee_and_ref_cells_mean_none(tmp_path):
    p = _write(tmp_path, "settlement,s1,100.00,INR,2026-08-26T09:00:00,,,,,UTR-9,,,")
    settlements, _ = load_batch_csv(p)
    assert settlements[0].fees is None
    assert settlements[0].refs.utr == "UTR-9"
    assert settlements[0].refs.order_id is None


def test_row_with_no_refs_at_all_loads(tmp_path):
    # all ref columns blank -> refs is empty -> TransactionRefs() (covers the no-refs branch)
    p = _write(tmp_path, "settlement,s1,100.00,INR,2026-08-25T09:00:00,,,,,,,,")
    settlements, _ = load_batch_csv(p)
    assert settlements[0].refs.utr is None and settlements[0].refs.order_id is None
    assert settlements[0].fees is None


def test_fee_columns_build_fees(tmp_path):
    p = _write(tmp_path, "settlement,s1,100.00,INR,2026-08-26T09:00:00,2.00,,,,UTR-1,,,")
    settlements, _ = load_batch_csv(p)
    assert settlements[0].fees.total() == Money.of("2.00", "INR")   # missing fee parts default to 0


def test_unknown_record_type_rejected(tmp_path):
    p = _write(tmp_path, "widget,s1,100.00,INR,2026-08-26T09:00:00,,,,,U1,,,")
    with pytest.raises(BatchLoadError):
        load_batch_csv(p)


def test_missing_required_cell_surfaces_error(tmp_path):
    # blank gross_amount -> omitted -> load_batch reports 'gross_amount is required'
    p = _write(tmp_path, "settlement,s1,,INR,2026-08-26T09:00:00,,,,,U1,,,")
    with pytest.raises(BatchLoadError):
        load_batch_csv(p)


def test_missing_record_type_column(tmp_path):
    p = tmp_path / "noheader.csv"
    p.write_text("id,gross_amount\ns1,100.00\n", encoding="utf-8")
    with pytest.raises(BatchLoadError):
        load_batch_csv(p)


def test_empty_file_no_header(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    with pytest.raises(BatchLoadError):
        load_batch_csv(p)


def test_missing_file(tmp_path):
    with pytest.raises(BatchLoadError):
        load_batch_csv(tmp_path / "nope.csv")


def test_bad_amount_still_rejected(tmp_path):
    p = _write(tmp_path, "settlement,s1,not-a-number,INR,2026-08-26T09:00:00,,,,,U1,,,")
    with pytest.raises(BatchLoadError):
        load_batch_csv(p)
