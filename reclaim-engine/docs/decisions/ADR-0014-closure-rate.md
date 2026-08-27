# ADR-0014 — Closure rate is a separate metric from match rate

**Status:** Accepted · 2026-08-28
**Context phase:** post-Sprint-1 review

## Context

`RunReport.match_rate()` is `matched_amount / total_expected`, where
`matched_amount` sums the expected payout of *reconciled* settlements only.
Recovered money is tracked in a separate `recovered_amount` field and never
enters the rate.

A review caught the consequence: the demo recovers ₹200, yet the reported rate
stays `0.6299`. Nothing in the engine's output moves when recovery succeeds.
That is a poor showing for a system whose thesis is "close the loop and prove
it", and it does not support a before/after closure claim.

## Decision

Add a **second** metric rather than redefining the first.

- `match_rate()` — unchanged. Detection *before* recovery: how much tied out on
  its own. This is the baseline.
- `closure_rate()` — `(matched + recovered) / total_expected`. The loop is
  closed for every rupee that either reconciled or was won back.
- `closed_amount()` — `matched + recovered`, the numerator, exposed so callers
  never re-derive it.

Both appear in `summary()`, the CLI report, and the HTML dashboard, presented as
a pair: `detection X% → closure Y%`.

## Why not fold recovery into `match_rate`

1. It is a silent breaking change to a metric that already has a precise,
   defensible meaning ("this tied out deterministically").
2. It destroys the very comparison that proves the product's value. One number
   cannot show that recovery moved anything; the *pair* is the evidence.
3. Matching and recovery are different claims with different confidence. A match
   is arithmetic; a recovery is money that moved through a payment rail.
   Collapsing them into one figure hides that distinction exactly where a reader
   most needs it.

## Consequences

- `summary()` gains `closed` and `closure_rate` keys — additive, so existing
  consumers keep working.
- Residual stays the honest remainder: `total_expected − closed` is what a human
  still owns, and the dashboard shows it as the unfilled part of the meter.
- **Any published closure figure must name the batch it came from.** The rate is
  a property of a specific run, not a benchmark. On the sample batch it is
  `0.6299 → 0.6496`.

## Tested by

`tests/test_pipeline.py`, `tests/test_cli.py::test_cli_reports_closure_rate`,
`tests/test_dashboard.py::test_dashboard_shows_closure_story`.
