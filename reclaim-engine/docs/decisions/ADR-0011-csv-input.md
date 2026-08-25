# ADR-0011 — CSV input as a thin adapter over the JSON loader

**Status:** Accepted · **Date:** 2026-08-26

## Context
Real settlement/bank data often arrives as CSV. We want CSV input without
duplicating the strict validation already in `batch_io`.

## Decision
`csv_io.load_batch_csv(path)` reshapes a single CSV (one row per record, a
`record_type` column of `settlement`/`bank`) into the canonical dicts that
`batch_io.load_batch` already validates, then delegates. CLI adds `--csv FILE`.

- One CSV, columns: `record_type,id,gross_amount,currency,ts` + optional fee
  columns (`mdr,gst_on_mdr,tcs,other`) + optional ref columns
  (`utr,rrn,order_id,invoice_no`). Blank cells are omitted (→ "required" errors
  or `None` refs / no fees), so the JSON loader's messages apply unchanged.
- Because CSV cells are strings, amounts stay strings and **never become
  floats** — the no-float rule holds for free.

## Consequences
- All validation (amounts-as-strings, ISO timestamps, domain invariants) is
  reused, so CSV and JSON inputs are guaranteed to produce identical objects —
  asserted by a test comparing the shipped `sample_batch.csv` and
  `sample_batch.json`.
- Single-file format with a `record_type` column keeps it simple; multi-file
  (`--settlements-csv/--bank-csv`) can be added later if needed.

## Alternatives considered
- **A separate CSV validator** — rejected (duplicates logic, risks divergence
  from the JSON path and the no-float guarantee).
- **Two CSV files instead of a `record_type` column** — deferred; one file is
  simpler and sufficient.
