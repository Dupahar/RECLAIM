"""Batch loading — the ingestion/normalization seam (deterministic, no LLM).

Parses a JSON batch into canonical :class:`~reclaim.domain.Transaction`
objects, validating strictly: amounts must be *strings* (so they become exact
``Money`` — never parsed as float), timestamps must be ISO-8601, and unknown
shapes are rejected with a clear ``BatchLoadError``. This is the small,
rule-based front door; the architecture's LLM-assisted extraction for messy
real-world formats would sit behind this same canonical output.

Expected JSON shape::

    {
      "settlements": [
        {"id": "s1", "gross_amount": "5000.00", "currency": "INR",
         "ts": "2026-08-25T09:00:00",
         "fees": {"mdr": "100.00", "gst_on_mdr": "18.00", "tcs": "0", "other": "0"},
         "refs": {"utr": "UTR-1", "order_id": "ORD-1"}}
      ],
      "bank_credits": [
        {"id": "b1", "gross_amount": "4882.00", "currency": "INR",
         "ts": "2026-08-25T09:00:00", "refs": {"utr": "UTR-1"}}
      ]
    }
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime

from .domain import Fees, Source, Transaction, TransactionRefs
from .money import Money, MoneyError


class BatchLoadError(Exception):
    """Raised when a JSON batch is malformed or fails validation."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise BatchLoadError(msg)


def _money(value, currency: str, where: str) -> Money:
    _require(isinstance(value, str), f"{where}: amount must be a string (got {type(value).__name__})")
    try:
        return Money.of(value, currency)
    except MoneyError as exc:
        raise BatchLoadError(f"{where}: invalid amount {value!r} ({exc})") from exc


def _fees(raw, currency: str, where: str) -> Fees:
    _require(isinstance(raw, dict), f"{where}: fees must be an object")
    return Fees(
        mdr=_money(raw.get("mdr", "0"), currency, f"{where}.mdr"),
        gst_on_mdr=_money(raw.get("gst_on_mdr", "0"), currency, f"{where}.gst_on_mdr"),
        tcs=_money(raw.get("tcs", "0"), currency, f"{where}.tcs"),
        other=_money(raw.get("other", "0"), currency, f"{where}.other"),
    )


def _refs(raw, where: str) -> TransactionRefs:
    if raw is None:
        return TransactionRefs()
    _require(isinstance(raw, dict), f"{where}: refs must be an object")
    for k, v in raw.items():
        _require(v is None or isinstance(v, str), f"{where}.{k}: reference values must be strings")
    return TransactionRefs(
        order_id=raw.get("order_id"),
        utr=raw.get("utr"),
        rrn=raw.get("rrn"),
        invoice_no=raw.get("invoice_no"),
    )


def _txn(raw, source: Source, where: str) -> Transaction:
    _require(isinstance(raw, dict), f"{where}: each record must be an object")
    _require(isinstance(raw.get("id"), str) and raw["id"] != "", f"{where}: 'id' is required")
    _require(isinstance(raw.get("currency"), str), f"{where}: 'currency' is required")
    currency = raw["currency"]
    _require("gross_amount" in raw, f"{where}: 'gross_amount' is required")
    _require(isinstance(raw.get("ts"), str), f"{where}: 'ts' (ISO-8601 string) is required")
    try:
        ts = datetime.fromisoformat(raw["ts"])
    except ValueError as exc:
        raise BatchLoadError(f"{where}: invalid ISO-8601 timestamp {raw['ts']!r}") from exc

    try:
        fees = _fees(raw["fees"], currency, f"{where}.fees") if raw.get("fees") is not None else None
        return Transaction(
            id=raw["id"], source=source,
            gross_amount=_money(raw["gross_amount"], currency, f"{where}.gross_amount"),
            ts=ts, fees=fees, refs=_refs(raw.get("refs"), f"{where}.refs"),
            counterparty=raw.get("counterparty"), narration_raw=raw.get("narration_raw"),
        )
    except BatchLoadError:
        raise
    except Exception as exc:  # domain validation error (e.g. negative fee) -> load error
        raise BatchLoadError(f"{where}: {exc}") from exc


def load_batch(data) -> tuple[list[Transaction], list[Transaction]]:
    """Validate a parsed JSON object into (settlements, bank_credits)."""
    _require(isinstance(data, dict), "batch must be a JSON object")
    _require(isinstance(data.get("settlements"), list), "'settlements' must be a list")
    _require(isinstance(data.get("bank_credits"), list), "'bank_credits' must be a list")
    settlements = [_txn(r, Source.SETTLEMENT, f"settlements[{i}]")
                   for i, r in enumerate(data["settlements"])]
    banks = [_txn(r, Source.BANK, f"bank_credits[{i}]")
             for i, r in enumerate(data["bank_credits"])]
    return settlements, banks


def load_batch_file(path) -> tuple[list[Transaction], list[Transaction]]:
    """Read and validate a JSON batch file."""
    p = pathlib.Path(path)
    if not p.exists():
        raise BatchLoadError(f"file not found: {path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BatchLoadError(f"invalid JSON in {path}: {exc}") from exc
    return load_batch(data)
