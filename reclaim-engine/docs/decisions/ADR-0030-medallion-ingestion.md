# ADR-0030 — A validating boundary is not a data platform

**Status:** Accepted · 2026-08-31 · implements architecture §4 (Layer 1, Data Platform)

## Context

Layer 1 was the least-implemented layer in the engine, around 10%, and the reason
was that `batch_io` and `csv_io` looked like enough. They are a *validating
boundary*: they parse a well-formed file into canonical objects and raise on
anything else. The difference between that and a platform showed up in three
places, all of which cost money rather than tidiness.

**Nothing was replayable from source.** A malformed row raised and the batch
stopped. There was no immutable record of what actually arrived, so "re-run
yesterday's file and get the same answer" was a property of the file still
sitting on disk rather than of the system.

**A rejected row disappeared entirely.** Goal G2 is *"nothing leaves the system
undocumented"*, and a row that failed validation left no trace — the single
category of data most likely to be hiding money, discarded with an exception
message.

**Reference normalisation had nowhere to live, and its absence fails silently.**
A settlement export writes `UTR000123456789`. The bank statement writes it inside
a narration: `NEFT-UTR000123456789-ACME`. Those are the same reference spelled
two ways. Left unnormalised the exact matcher finds nothing, the batch reports a
0% match rate, and **no error is raised anywhere** — the failure mode a
validating boundary is structurally unable to catch, because both files are
individually valid. This actually happened while building the layer: the first
end-to-end run reported 0% matched with every row conformed.

## Decision

An `ingest` module implementing Bronze → Silver → Gold, plus `--pg-csv` and
`--bank-csv` on the CLI.

**Bronze is content-addressed and immutable.** Each row lands exactly as
received with `batch_id`, `source`, `line_no` and a SHA-256 of its canonical
form. Content addressing rather than filename tracking means the same delivery
under a different name is recognised as already known — and a replay can prove it
read the same bytes. Duplicates are *returned*, not discarded, so a caller can
report how much of a delivery was already held.

**Silver's invariant is arithmetic and checkable.** Every landed row ends up in
exactly one of `transactions` or `quarantined`, and `accounted_for()` proves it.
This is G2 expressed as a count that a test can assert, rather than as an
intention. A quarantine record keeps its Bronze row, its line number, a
machine-readable `rule` and a human reason, so the person who has to fix the file
is told what to fix.

**Normalisation is part of conforming, not a nicety.** `normalise_utr` uppercases,
drops separators and strips a leading `UTR` label, and both adapters run their
reference through it. The original spelling is kept in `evidence`, so normalising
loses nothing and the transformation is auditable.

**Extraction reports a confidence and refuses below a threshold.** The regex
extractor returns 1.0 for an explicitly labelled `UTR…` — the label removes the
ambiguity — and 0.75 for a bare run after a `NEFT`/`IMPS`/`UPI`/`RTGS` prefix,
because that shape also appears in account numbers and internal ids. Anything
else returns 0 and is quarantined. The confidences are a claim about *this rule
set*, not a probability from a model, and the docstring says so.

Refusing is the whole point: **a wrong reference here does not fail loudly, it
silently creates a false match**, which is worse than a missing row. This is the
same stance the gated resolver takes on an uncertain match, applied at ingestion.

**Fees are unpacked at the boundary** so nothing downstream ever sees a gross
amount it could mistake for a payout — the architecture's *"unpack MDR /
GST-on-MDR / TCS before anything downstream sees them"*.

**Only credits are ingested from a statement.** A debit on the merchant's account
is not a settlement payout, and admitting one would invent money. The refusal
message says that rather than just "invalid direction".

**Duplicate UTRs are caught in Gold, where the line number is still known.**
`reconcile_settlements_to_bank` raises on a duplicate UTR — correct, but it
surfaces from the middle of the pipeline with the whole batch lost. Gold detects
the collision and quarantines **both** rows, because keeping the first would be a
silent choice about which is real, resolved by arrival order. A duplicate UTR
usually means a double delivery or a broken export, and a human should see it.

## What this deliberately does not do

**There is no Kafka, no CDC, and no streaming.** The architecture's case for them
— *"financial data arrives as small continuous updates instead of bulky daily
uploads"* — is unaddressed. This is batch ingestion with medallion *semantics*:
lineage, immutability, quarantine and replay. Those are the properties that have
to be right whatever moves the bytes, but the layer is not near-real-time and
does not pretend to be. Goal **G10 remains outstanding**.

**Bronze is in memory.** It is content-addressed and replayable within a process
and does not survive a restart — the same gap [ADR-0029](ADR-0029-closing-sprint-3-caveats.md)
just closed for conduct state, reappearing one layer down. A `BronzeRepository`
following the existing pattern is the obvious next step; claiming the layer is
durable before it exists would be exactly the overreach ADR-0028 got wrong.

**No Gold aggregates.** Gold here is the reconciliation-ready *split*, validated
against the matcher's preconditions. The architecture's "aggregated views" are
not built, because nothing consumes them yet.

**No Account Aggregator, no PDF, no LLM extractor.** AA is an integration, not a
module. PDF statements are the hardest part of the real tail and are absent. The
`NarrationExtractor` seam exists with a deterministic implementation behind it,
exactly as `ChatClient` is for the resolver — and, as there, **no model is
called**.

**Two formats, not the Indian long tail.** A Razorpay-shaped settlement export
and one bank-narration statement. PayU, Cashfree and the per-bank narration
dialects are not covered, and the regex is not calibrated against real
statements.

## Tested by

`tests/test_ingest.py` (38) and 9 in `tests/test_cli.py`. The load-bearing ones
are the invariant and the refusals: `test_every_landed_row_is_accounted_for`,
`test_quarantine_is_totalled_by_cause`,
`test_normalisation_makes_both_spellings_of_one_reference_agree` (the bug that
actually occurred), `test_a_low_confidence_extraction_is_refused_rather_than_guessed`,
`test_a_debit_is_refused_because_treating_it_as_a_payout_invents_money`,
`test_both_sides_of_a_collision_are_quarantined`,
`test_the_same_file_landed_twice_produces_no_new_records`,
`test_re_landing_a_file_changes_nothing_downstream`, and
`test_lineage_survives_all_the_way_to_a_leak` — a leak traced back to the file
and line it came from.

**Dead code removed by the discipline.** Two `"normalises to nothing"` guards
were written and then proved unreachable: `normalise_utr` raises on a value that
reduces to nothing, because an empty string is not alphanumeric. Deleted rather
than kept alive by tests written to reach them — the same lesson as the Phase 3
unreachable balance guard and `CellStats.merge`.
