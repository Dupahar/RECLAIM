"""CSV input adapter.

A thin front end over ``batch_io``: a single CSV (one row per record, a
``record_type`` column of ``settlement``/``bank``) is reshaped into the same
canonical dicts that ``batch_io.load_batch`` validates. Because CSV cells are
strings, amounts stay strings and never risk becoming floats — the no-float
rule holds for free.

Columns (header required):
    record_type,id,gross_amount,currency,ts,
    mdr,gst_on_mdr,tcs,other,          # fee columns (optional; blank = none)
    utr,rrn,order_id,invoice_no        # reference columns (optional; blank = none)
"""
from __future__ import annotations

import csv
import pathlib

from .batch_io import BatchLoadError, load_batch

_CORE = ("id", "gross_amount", "currency", "ts")
_FEES = ("mdr", "gst_on_mdr", "tcs", "other")
_REFS = ("utr", "rrn", "order_id", "invoice_no")


def _cell(row: dict, key: str):
    v = row.get(key)
    return v.strip() if isinstance(v, str) else v


def _record_from_row(row: dict) -> dict:
    rec: dict = {}
    for k in _CORE:
        val = _cell(row, k)
        if val:                       # omit blanks so load_batch reports "required"
            rec[k] = val
    fees = {k: _cell(row, k) for k in _FEES if _cell(row, k)}
    if fees:
        rec["fees"] = fees
    refs = {k: _cell(row, k) for k in _REFS if _cell(row, k)}
    if refs:
        rec["refs"] = refs
    return rec


def load_batch_csv(path):
    """Parse a CSV batch into (settlements, bank_credits), validated via batch_io."""
    p = pathlib.Path(path)
    if not p.exists():
        raise BatchLoadError(f"file not found: {path}")
    text = p.read_text(encoding="utf-8")
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None or "record_type" not in reader.fieldnames:
        raise BatchLoadError("CSV must have a header row including a 'record_type' column")

    settlements: list[dict] = []
    banks: list[dict] = []
    for i, row in enumerate(reader):
        where = f"row {i + 2}"  # +2: 1-based, past the header
        rt = (_cell(row, "record_type") or "").lower()
        rec = _record_from_row(row)
        if rt == "settlement":
            settlements.append(rec)
        elif rt == "bank":
            banks.append(rec)
        else:
            raise BatchLoadError(f"{where}: unknown record_type {rt!r} (expected 'settlement' or 'bank')")

    return load_batch({"settlements": settlements, "bank_credits": banks})
