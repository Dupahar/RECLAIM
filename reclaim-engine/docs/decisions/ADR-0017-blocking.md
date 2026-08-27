# ADR-0017 — Blocking is derived from the scoring config, so it cannot lose a match

**Status:** Accepted · 2026-08-28 · implements architecture §6 stage 1

## Context

`probabilistic_match` compared every settlement against every bank credit — a
literal nested loop. At 10k x 10k that is 100M `Decimal` scorings, which was the
practical ceiling on batch size and the single hardest blocker to running the
engine on a real merchant's data.

Entity resolution solves this with **blocking**: only compare records sharing a
blocking key. The usual cost is recall — a blocking key is a heuristic, and a
true match whose records land in different blocks is silently lost. For money,
"silently lost" is not an acceptable trade.

## Decision

Derive the block from the scoring configuration instead of guessing one.

A pair scores `w_a*a + w_d*d + w_r*r` with every component in `[0, 1]`. Even
with perfect date and reference agreement, a pair still needs

    w_a * a  >=  review_threshold - w_d - w_r

so any pair whose amount score falls below `a_min = (T - w_d - w_r) / w_a` is
unreachable **whatever every other field says**. With the default weights
(0.5 / 0.2 / 0.3) and `T = 0.6`, `a_min = 0.2`: amounts must agree to within
0.8%. That is a narrow band, and a sorted list plus two binary searches finds it.

Three properties make this safe rather than merely fast:

1. **Provably lossless.** The block is a strict superset of what scoring would
   accept, so the result is identical to comparing every pair.
2. **The window is widened by one score quantum.** `amount_score` rounds to 4 dp
   and a pair sitting a hair below the bound can round *up* onto it. Too wide
   costs a comparison; too narrow loses a match.
3. **It disables itself when it cannot be sound.** If `a_min <= 0` — a weighting
   where date and reference alone can clear the threshold — no amount block
   exists and the matcher compares every pair. The optimisation is skipped
   rather than silently lossy.

Blocks never span currencies, because a currency mismatch scores 0 on amount.

## Consequences

- 2000 x 2000 goes from 4,000,000 scorings to 63,688 (1.6%), in 0.27s.
- `ProbabilisticResult` reports `pairs_compared` / `pairs_total`, so the saving
  is visible rather than asserted.
- Changing the weights changes the block automatically. This is why the
  derivation lives in code (`min_amount_score`) rather than a tuned constant.
- Still O(N*M) in the worst case, by design: a batch where every amount is
  identical has no structure to exploit and must be compared in full.

## Tested by

`tests/test_probabilistic.py` — `test_blocking_is_lossless_against_brute_force`
runs a seeded corpus of near-miss amounts straddling the tolerance edge through
both the blocked matcher and an independent brute-force oracle and requires
identical auto/review sets; `test_blocking_lossless_under_alternative_weights`
repeats it for a different weighting; plus zero-amount, cross-currency,
no-sound-block and saving-reported cases.
