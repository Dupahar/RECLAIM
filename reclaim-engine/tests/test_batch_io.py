"""Phase 9 tests — batch loader (ingestion/normalization)."""
import json

import pytest

from reclaim.money import Money
from reclaim.domain import Source
from reclaim.batch_io import BatchLoadError, load_batch, load_batch_file


def _valid():
    return {
        "settlements": [
            {"id": "s1", "gross_amount": "5000.00", "currency": "INR",
             "ts": "2026-08-25T09:00:00",
             "fees": {"mdr": "100.00", "gst_on_mdr": "18.00"},
             "refs": {"utr": "UTR-1", "order_id": "ORD-1"}},
        ],
        "bank_credits": [
            {"id": "b1", "gross_amount": "4882.00", "currency": "INR",
             "ts": "2026-08-25T09:00:00", "refs": {"utr": "UTR-1"}},
        ],
    }


def test_load_valid_batch():
    settlements, banks = load_batch(_valid())
    assert len(settlements) == 1 and len(banks) == 1
    s = settlements[0]
    assert s.source is Source.SETTLEMENT
    assert s.gross_amount == Money.of("5000.00", "INR")
    assert s.fees.total() == Money.of("118.00", "INR")   # tcs/other default to 0
    assert s.refs.utr == "UTR-1" and s.refs.order_id == "ORD-1"
    assert banks[0].refs.utr == "UTR-1"


def test_amounts_must_be_strings_not_numbers():
    data = _valid()
    data["settlements"][0]["gross_amount"] = 5000.00   # a float -> rejected
    with pytest.raises(BatchLoadError):
        load_batch(data)


def test_missing_top_level_keys():
    with pytest.raises(BatchLoadError):
        load_batch({"settlements": []})            # no bank_credits
    with pytest.raises(BatchLoadError):
        load_batch({"bank_credits": []})           # no settlements
    with pytest.raises(BatchLoadError):
        load_batch([])                             # not an object


def test_missing_required_fields():
    for drop in ("id", "currency", "gross_amount", "ts"):
        data = _valid()
        del data["settlements"][0][drop]
        with pytest.raises(BatchLoadError):
            load_batch(data)


def test_bad_timestamp():
    data = _valid()
    data["settlements"][0]["ts"] = "25-08-2026"
    with pytest.raises(BatchLoadError):
        load_batch(data)


def test_bad_amount_value():
    data = _valid()
    data["settlements"][0]["gross_amount"] = "not-a-number"
    with pytest.raises(BatchLoadError):
        load_batch(data)


def test_domain_error_surfaces_as_load_error():
    # negative fee -> domain rejects -> surfaced as BatchLoadError
    data = _valid()
    data["settlements"][0]["fees"] = {"mdr": "-5.00"}
    with pytest.raises(BatchLoadError):
        load_batch(data)


def test_refs_values_must_be_strings():
    data = _valid()
    data["settlements"][0]["refs"] = {"utr": 12345}
    with pytest.raises(BatchLoadError):
        load_batch(data)


def test_no_fees_and_no_refs_ok():
    data = {
        "settlements": [{"id": "s1", "gross_amount": "100.00", "currency": "INR",
                         "ts": "2026-08-25T09:00:00"}],
        "bank_credits": [],
    }
    settlements, banks = load_batch(data)
    assert settlements[0].fees is None
    assert settlements[0].refs.utr is None


def test_record_must_be_object():
    with pytest.raises(BatchLoadError):
        load_batch({"settlements": ["not-an-object"], "bank_credits": []})


# --------------------------------------------------------------------------
# File loading
# --------------------------------------------------------------------------
def test_load_batch_file(tmp_path):
    p = tmp_path / "batch.json"
    p.write_text(json.dumps(_valid()), encoding="utf-8")
    settlements, banks = load_batch_file(p)
    assert len(settlements) == 1 and len(banks) == 1


def test_load_batch_file_missing(tmp_path):
    with pytest.raises(BatchLoadError):
        load_batch_file(tmp_path / "nope.json")


def test_load_batch_file_bad_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(BatchLoadError):
        load_batch_file(p)


def test_sample_batch_file_loads():
    # the shipped example must always be valid
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    settlements, banks = load_batch_file(root / "examples" / "sample_batch.json")
    assert len(settlements) == 4 and len(banks) == 3
