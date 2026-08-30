# RECLAIM Engine — Build Log

A chronological record of what was built, how it was tested, and what is verified. Every "done" is backed by passing tests, with limitations and confidence stated explicitly.

Conventions: each entry records **What / Why / How tested / Result / Open items**.

---

## 2026-08-25 — Phase 0: Project scaffold & tooling

**What:** Created the `reclaim-engine` project (src layout): `src/reclaim/` package, `tests/`, `docs/`, `pyproject.toml` with pytest config (`pythonpath=src`, `--strict-markers`), README stating the engineering contract, and this build log. Added a smoke test.

**Why:** A strong foundation needs a reproducible test harness and documentation *before* any domain code, so every subsequent layer is proven the moment it is written.

**How tested:** `python -m pytest` — smoke test imports the package and checks the version.

**Result:** ✅ `1 passed in 0.06s` (pytest 9.0.2, Python 3.13.9). Toolchain, src-layout import, and config verified. pytest-cov is available for later coverage runs.

**Open items:** none for Phase 0. Next: Phase 1 (Money primitive).

---

## 2026-08-25 — Phase 1: Money primitive

**What:** `src/reclaim/money.py` — an immutable, `Decimal`-backed, currency-safe `Money` value type. Accepts only `int`/`str`/`Decimal` (rejects `float` and `bool` loudly); enforces same-currency arithmetic/comparison; preserves full precision through intermediate math and snaps to currency minor units via `round()` (default `ROUND_HALF_UP`); consistent equality/hashing across precisions (1.1 == 1.10); a small currency minor-unit registry (INR/USD/…/JPY, default 2).

**Why:** Goal **G1 (Correctness)**. A money-moving system must never use binary float. This type is the single choke-point that makes decimal exactness structural rather than a coding convention.

**How tested:** `tests/test_money.py` — 42 tests covering construction, float/bool rejection, non-finite rejection, currency validation, the classic `0.1+0.2` bug (proven avoided), equality/precision/hashing, add/sub/neg/abs/mul (incl. full-precision fee example 4783.20 × 2% = 95.6640), rounding (half-up, JPY 0-dp), ordering, immutability (frozen), currency registry, representations, and all error branches.

**Result:** ✅ `42 passed`, **100% line coverage** on `money.py` (`--cov=reclaim.money`). Re-run confirmed green.

**Open items / honest notes:**
- `allocate()` (splitting a sum into parts without losing sub-units) is **not yet implemented** — deferred until the ledger needs it (Phase 3+). Flagged so it isn't assumed present.
- Coverage is line-level; branch coverage not separately measured (acceptable — the failure-mode tests exercise both sides of each guard).

Next: Phase 2 (canonical domain model).

---

## 2026-08-25 — Phase 2: Canonical domain model

**What:** `src/reclaim/domain.py` — immutable value objects shared by every downstream layer:
- Enums: `Source`, `Direction`, `MatchStatus`, `LeakType`, `RecoveryState` (stable string values for audit/serialization).
- `Fees` (mdr / gst_on_mdr / tcs / other) — single-currency, non-negative, with `total()`.
- `Transaction` — id/source/gross_amount/ts + optional fees/refs; computed `net_amount = gross − fees.total()`; currency consistency + confidence-bound validation.
- `LedgerEntry` — one side of a double-entry posting; **amount must be positive and rounded** (sign is carried by `Direction`, no sub-unit dust postable).
- `LeakRecord` — a positive-magnitude unreconciled amount with type/hypothesis/confidence/recovery-state.

**Why:** A single, validated vocabulary prevents malformed data from ever reaching the ledger or reconciler. Invariants live in `__post_init__`, not in convention (goals **G2/G3**). IDs and timestamps are caller-provided — nothing non-deterministic is generated inside the domain (**G4**).

**How tested:** `tests/test_domain.py` — 24 tests: Fees total/zero/currency-mismatch/negative/type; Transaction net-amount (incl. settlement gross 5000 − fees 117.88 = 4882.12), currency match, id/type/datetime/confidence validation, refs defaults, immutability; LedgerEntry positivity + rounding + required-field invariants; LeakRecord positivity, source-ref typing, confidence bounds, defaults; enum value stability.

**Result:** ✅ `24 passed`, **100% coverage** on `domain.py`. Full-suite re-run: **66 passed, 100% coverage** across the package (`money` + `domain` + `__init__`).

**Open items / honest notes:**
- `match_confidence`/`confidence` are floats in [0,1] — floats are acceptable here because they are *probabilities, not money* (the no-float rule is money-specific).
- Domain objects are pure data; persistence/serialization is deliberately out of scope until a later phase.

Next: Phase 3 (double-entry ledger core).

---

## 2026-08-25 — Integration recheck: Phases 0+1+2 together

**What:** Verified the three foundation layers *compose*, not just pass in isolation. Added `tests/test_integration_foundation.py`:
1. **End-to-end settlement:** gross ₹5,00,000 → MDR 2% (₹10,000) + GST-on-MDR 18% (₹1,800) computed via `Money` multiply-then-round → `Fees.total()` ₹11,800 → `Transaction.net_amount` (payout) **₹4,88,200**. Modeled as three `LedgerEntry` objects (bank_account + fee_expense debits vs sales_clearing credit) and asserted **debits == credits == gross** — the double-entry invariant holding by construction across `Money` + `domain`.
2. **Failed-debit leak:** a ₹499 Autopay failure → well-formed recoverable `LeakRecord`.
3. **No-float pipeline:** exactness preserved end-to-end (0.10 + 0.20 == 0.30 through `Money` → `Transaction`), contrasted with the broken float result.

**Why:** "Recheck all three together" — a strong foundation must be coherent as a system before the ledger is built on top of it.

**How tested:** full suite re-run.

**Result:** ✅ **69 passed, 100% coverage** across the package (`money` + `domain` + `__init__`). No regressions; the layers integrate cleanly and the double-entry balance is exact.

**Verdict:** Foundation Phases 0–2 are **verified as an integrated whole.** Confidence: High (behaviour proven by tests, exactness demonstrated, invariants enforced in code). Cleared to build Phase 3.

---

## 2026-08-25 — Phase 3: Double-entry ledger core

**What:** `src/reclaim/ledger.py`:
- `Posting` — the atomic journal entry: ≥2 `LedgerEntry` lines, single currency, entry-ids unique, every `entry.txn_id == posting.id`, and **debits == credits** (else `UnbalancedPostingError`). Immutable.
- `Ledger` — append-only in-memory store: `post()` is **idempotent by id** (same id+content = no-op; same id, different content = `DuplicatePostingError`); `balance()` (debit-normal = debits − credits), `debit_total`/`credit_total`, `accounts()`, and `is_globally_balanced(currency)`. Deterministic: state is a pure function of the ordered postings.

**Why:** This is the "provable integrity" layer (goals **G3/G4/G5**). Balance is enforced at construction, idempotency prevents double-posting, immutability makes corrections additive, and determinism makes replay/audit possible.

**How tested:** `tests/test_ledger.py` — 20 tests: balanced posting; unbalanced rejected; ≥2-entry / single-currency / txn_id-match / unique-entry-id / required-field invariants; entries-must-be-tuple and all-LedgerEntry guards; immutability; post + read-back; **idempotent repost**; same-id-different-content rejected; debit-normal balances (bank +4,88,200 / fee +11,800 / sales −5,00,000); debit/credit totals; global balance; **multi-currency isolation**; and **deterministic replay** (two ledgers from the same postings → identical balances).

**Result:** ✅ `20 passed`, **100% coverage** on `ledger.py`. Full-suite re-run: **89 passed, 100% coverage** across the package.

**Design issue caught by the discipline (honest note):** my first draft had a defensive `if debits.is_zero: raise` guard in `Posting`. The first test run proved it **unreachable** — `LedgerEntry` already forbids non-positive amounts, so a balanced posting with ≥2 positive entries *always* totals > 0. I **removed the dead guard** and replaced the test with one asserting the real behaviour (zero-amount entries are rejected at the domain layer; zero postings are impossible by construction). This is exactly why we test before moving on — it corrected a false assumption and kept the foundation honest and fully-covered.

**Open items:** durable/event-sourced storage is deferred (in-memory reference implementation); the invariants here are the contract a storage layer must preserve. Balance is debit-normal and type-agnostic by design — account-type interpretation lives above the ledger.

Next: Phase 4 (deterministic reconciliation core).

---

## 2026-08-25 — All-together verification (branch coverage)

**What:** Ran the entire suite as one unit with **branch** coverage enabled (`--cov-branch`), addressing the Phase-1 open item that only line coverage had been measured.

**Result:** ✅ **89 passed**, **100% line AND branch coverage** (332 statements, 68 branches, 0 partial) across `money` + `domain` + `ledger` + `__init__`. The foundation is verified as a coherent whole with no untested branches. Confidence: High. Cleared to build Phase 4.

---

## 2026-08-25 — Phase 4: Deterministic reconciliation core

**What:** `src/reclaim/reconciliation.py` — the first real RECLAIM capability, deliberately **AI-free** (exact rules only; fuzzy/AI matching is a later layer above this gate). `reconcile_settlements_to_bank(settlements, bank_credits)` matches settlement payouts (`gross − MDR − GST − TCS`) against bank credits by **exact UTR + amount**, producing a `ReconciliationResult` with matched pairs, a typed `LeakRecord` list, and honest rates (`match_rate_by_count`/`by_value` as `Decimal`, never float). Leak taxonomy: `MISSING_SETTLEMENT`, `SHORT_PAYMENT` (recoverable), over-credit `UNEXPLAINED_FEE`, unexpected-credit `TIMING`. Input validation rejects missing/duplicate UTRs, currency mixing, and wrong sources.

**Why:** Detection is the entry point of the loop; it must be deterministic and honest (goals **G2/G4**). Nothing reconciled is silently dropped — everything becomes a matched pair or a typed, hypothesised leak.

**How tested:** `tests/test_reconciliation.py` — 13 tests: all-match; missing-settlement with exact rate checks (98% by value, 66.67% by count); settlement-decomposition-driven match (5000 − 117.88 fees → 4882.12); short-payment (recoverable) / over-credit / unexpected-credit leaks; `leaked_amount` totals; determinism (identical results on re-run); and all error paths (missing/duplicate UTR, currency mix, wrong source, empty input).

**Bug caught by the tests (honest note):** first draft referenced `bank.amount`, but `Transaction` exposes `gross_amount`/`net_amount` — 7 tests failed immediately with `AttributeError`. Fixed to use `bank.gross_amount` (a bank credit has no fees, so gross == credited). A second gap (`leaked_amount()` uncovered) was closed with a dedicated test. This is the "test before moving on" contract doing its job.

**Result:** ✅ `13 passed`, **100% line + branch** on `reconciliation.py`.

### Capstone integration — the whole foundation as one loop
**What:** `tests/test_capstone_reconcile_to_ledger.py` — reconcile → post matched settlements to the double-entry `Ledger` → emit the honest 3-number summary. Verifies: matched 2 / leaks 2; `matched_amount` ₹7,882.12; `leaked_residual` ₹1,500.00; `match_rate_by_value` 0.8874; ledger globally balanced; `bank_account` balance == matched amount; `sales_clearing` −8,000; `fee_expense` +117.88. Plus an **idempotent re-process** test (running the same batch twice does not double-post).

**Result:** ✅ full suite **104 passed, 100% line + branch coverage** (428 statements, 106 branches, 0 partial) across all five modules.

**Foundation verdict:** Phases 0–4 complete and verified as an integrated whole. The deterministic, AI-free bedrock — exact money, validated domain, provable double-entry ledger, and honest reconciliation — is done. **Confidence: High** (behaviour proven by 104 tests at 100% line+branch coverage; two real bugs caught and fixed by the discipline). This is the stable base the AI layers (probabilistic matching, exception resolver, recovery prediction, orchestration) will sit *above*.

**Open items for the next stage (beyond the foundation):** durable/event-sourced persistence; probabilistic linkage (Fellegi-Sunter) + AI exception resolver with verifier; recovery engine (diagnosis, funded-moment prediction, bandit) as Temporal sagas; Merkle transparency audit log; ingestion/normalisation connectors.

---

## 2026-08-25 — Phase 5: Probabilistic (fuzzy) matching layer

**What:** `src/reclaim/probabilistic.py` — the "second brain" that runs *after* the exact gate on the residual (no-UTR-match) settlements/banks. A transparent, deterministic, `Decimal`-based Fellegi-Sunter-style scorer:
- Field scorers → [0,1]: `amount_score` (exact = 1, linear decay to 0 at a relative `amount_tolerance`), `date_score` (same-day = 1, decays to 0 at `max_days`), `reference_score` (1 if any non-empty ref field matches).
- Weighted total (`amount 0.5 / date 0.2 / reference 0.3`, weights validated to sum to 1) → three bands: **auto** (≥ match_threshold 0.9), **review** (≥ review_threshold 0.6; for the future AI/human resolver), **residual** (stays a leak).
- Greedy **one-to-one** assignment with fully deterministic ordering (score desc, then id asc). Config + weights are validated; the layer never auto-acts.
- Also: additive change to Phase 4 — `ReconciliationResult` now exposes `unmatched_settlements` / `unmatched_bank_credits` (the no-UTR-match residual the probabilistic layer consumes). Short/over-payment cases are excluded (they are UTR-matched genuine discrepancies). Rationale recorded in **ADR-0002**.

**Why:** Reduces false leaks by recovering matches the exact matcher can't see (timing, garbled UTR, alternate refs) — while staying explainable, calibratable, and 100%-testable (no external lib, no LLM). This is the architecture's two-brain design, deterministic half complete.

**How tested:** `tests/test_probabilistic.py` — 17 tests: weight/threshold/tolerance/max_days config validation; each field scorer incl. currency guard, zero-expected, within/beyond tolerance, same/near/far dates, `max_days=0`, ref agreement; `score_pair` combinations (1.0 and 0.7); auto/review/residual banding; **one-to-one greedy** (two settlements contend for one bank → deterministic winner); determinism; empty input. Capstone `tests/test_capstone_two_brain.py` — a garbled-UTR pair the exact matcher splits into two leaks is recovered by the probabilistic layer as a single 0.70 **review** candidate (residual empty).

**Correctness note (caught while writing):** used `abs(expected.amount)` in the relative-difference denominator so a pathological negative net can't push the score above 1 — which also removed an otherwise-unreachable upper clamp (keeping 100% branch coverage honest, same lesson as Phase 3).

**Result:** ✅ `probabilistic.py` **17 tests, 100% line + branch**. Phase-4 additive change re-tested (no regression). Full suite: **122 passed, 100% line + branch coverage** (541 statements, 142 branches, 0 partial) across all six modules.

**Open items:** exact-field reference agreement only (fuzzy string similarity is a future field scorer); fixed weights (EM-estimated weights once labelled outcome data exists); the **review band** is where the AI exception resolver (next layer) will plug in.

Next candidate layers: durable persistence; the **AI exception resolver** (first LLM layer, gated — consumes the review band); recovery engine.

---

## 2026-08-25 — Phase 6: Gated AI exception resolver (first LLM layer)

**What:** `src/reclaim/resolver.py` — adjudicates the Phase-5 review band. The AI enters here, but **gated and behind an interface** (ADR-0003):
- `ExceptionResolver` protocol (`assess -> Assessment`) — the seam a real LLM implements; tests use deterministic fakes (`StaticResolver`, `SequenceResolver`, `RaisingResolver`).
- `GatedResolver` applies **self-consistency** (N samples, strict majority), a conservative **confidence gate** (weakest supporting sample must clear `accept_confidence`), and an **adversarial verifier** (second opinion prompted to refute). Outcomes: `CONFIRMED_MATCH` / `REJECTED` / `ESCALATE_HUMAN`.
- **Safe degradation (G6):** no majority, low confidence, verifier refutation, or *any* `ResolverError` → `ESCALATE_HUMAN`. Never guesses; never moves money (confirmation is an input to a later gated posting). Every outcome carries its samples for audit. `Assessment.confidence` is `Decimal` (no-float discipline).

**Why:** The 2026 verification thesis, made literal — an AI proposal is only trusted after consensus + confidence + refutation-survival, and fails safe otherwise. Building it behind an interface with fakes keeps this non-deterministic layer 100%-testable offline.

**How tested:** `tests/test_resolver.py` — 14 tests: Assessment/config validation (incl. float-confidence rejection); confirmed-match with/without verifier; rejected on negative consensus; escalation on no-consensus (even-split tie), low confidence, verifier refutation, base-resolver error, verifier error; odd-n majority-true confirm & majority-false reject; SequenceResolver exhaustion; batch + determinism. Capstone `tests/test_capstone_full_pipeline.py` — exact gate → probabilistic review band → gated resolver **confirms** the garbled-UTR match (fake model consensus+verified) and **escalates** when the model is unsure.

**Result:** ✅ `resolver.py` 15 tests, **100% line + branch**. Full suite: **138 passed, 100% line + branch coverage** (644 statements, 164 branches, 0 partial) across all seven modules.

**Open items / honest notes:** no real LLM implementation yet — that is a future `ExceptionResolver` drop-in (requires API access; its stochasticity is contained by the gate, its failures by safe degradation). Confidence in the *gating logic*: High (fully tested). Confidence that a real LLM will behave well: to be measured with real data/prompts (calibration is future work).

Next candidate layers: durable persistence; recovery engine (diagnosis + funded-moment prediction + bandit) as Temporal sagas; Merkle transparency audit log; ingestion connectors.

---

## 2026-08-25 — Phase 7: Bounded recovery engine (the "reclaim" half)

**What:** `src/reclaim/recovery.py` — acts on a recoverable leak:
- `diagnose(reason)` — rule-based root cause: TEMPORARY (insufficient funds / bank downtime / soft decline), PERMANENT (mandate revoked / hard decline), UNKNOWN.
- `RecoveryEngine.recover(leak, reason, base_time)` — a deterministic, bounded, compliant orchestrator. PERMANENT → `NOT_RECOVERABLE` (not chased); UNKNOWN → `HALTED` (escalate); TEMPORARY → up to `max_attempts` retries, the **first only after the RBI 24h pre-debit notice**, spaced by `gap_hours`, each with a deterministic idempotency key (`{leak.id}:attempt:{k}`), rotating channels. Success → `RECOVERED` (+recovered amount); all fail → `EXHAUSTED` (stopping rule); executor error → `HALTED`. Full attempt log for audit.
- External effect behind the `RecoveryExecutor` protocol (real UPI/gateway/WhatsApp later); tests use deterministic fakes. Added `RecoveryState.HALTED` to the domain (additive). Rationale in **ADR-0004**.

**Why:** Recovery touches money, so compliance and bounds must be structural (encoded), not advisory — and it must never double-debit, harass a dead mandate, or guess. Building it deterministically behind an executor interface keeps it 100%-testable.

**How tested:** `tests/test_recovery.py` — 18 tests: diagnosis table; config validation; permanent-not-chased (0 attempts); unknown-halts; recovered-on-first-attempt with the **notice window** enforced (first attempt at base+24h) and idempotency key; recovered-on-second; exhaustion at the attempt cap; executor-error → HALTED; multi-channel rotation; `max_attempts` respected; determinism; base_time type guard; SequenceExecutor exhaustion. Capstone `tests/test_capstone_recovery_to_ledger.py` — reconcile detects a ₹50 short payment → recovery reclaims it → recovered ₹50 posted as a balanced double-entry (ledger globally balanced).

**Result:** ✅ `recovery.py` 18 tests, **100% line + branch**. Domain re-tested after the additive `HALTED` (no regression). Full suite: **157 passed, 100% line + branch coverage** (759 statements, 188 branches, 0 partial) across all eight modules.

**Open items:** funded-moment prediction + contextual bandit (timing × channel × message) are future executor/planner strategies that slot into the bounded skeleton unchanged; a real `RecoveryExecutor` (with end-to-end idempotency) is the production drop-in. Confidence: High on the orchestration logic (fully tested); the ML/predictor efficacy is future, data-dependent work.

**Milestone:** RECLAIM's full closed loop now exists deterministically end-to-end — **detect (exact + probabilistic + gated AI) → recover (bounded, compliant) → book (double-entry ledger)** — at 100% line+branch coverage, with a real LLM and real executor as clean drop-ins behind interfaces.

Next candidate layers: durable/event-sourced persistence; Merkle transparency audit log; ingestion/normalisation connectors; real LLM + real executor implementations.

---

## 2026-08-25 — Phase 8: Orchestration pipeline + runnable demo

**What:** `src/reclaim/pipeline.py` — `run_reclaim(settlements, banks, *, resolver=None, recovery_engine=None, reason_for=None, base_time=None, prob_config=...)` composes the whole loop: exact reconcile → probabilistic on residual → gated resolver on review band (optional) → bounded recovery on recoverable leaks (optional) → post reconciled + recovered money to a fresh `Ledger`. Returns a frozen `RunReport` with the three numbers (match rate / matched / recovered), the **honest residual exception list**, AI + recovery audit, and the balanced ledger; `summary()` gives a display dict. Deterministic (injected resolver/executor, supplied `base_time`). ADR-0005. Also `src/reclaim/demo.py` — a runnable, reproducible end-to-end demo (`python -m reclaim.demo`).

**Why:** Turns the tested library into one usable capability with a clean, honest entry point — and proves all layers compose. Dependency injection keeps runs hermetic and supports capability tiers (with/without LLM, with/without recovery).

**How tested:** `tests/test_pipeline.py` — 11 tests: all-exact; full loop (exact + short-payment recovery + garbled-UTR→AI-confirm + genuine residual) with report-number assertions; no-resolver → pending review; no-recovery → shortfall stays; recovery requires base_time; AI escalation keeps residual; determinism; empty batch; plus branch-closing tests for fee posting, probabilistic **auto**-match via shared order_id, and **failed** recovery. `tests/test_demo.py` — smoke test asserts the demo's exact numbers (matched 6382, recovered 200, 1 residual = s4).

**Result:** ✅ **169 passed, 100% line + branch coverage** (888 statements, 230 branches, 0 partial) across all nine modules. `python -m reclaim.demo` runs the loop end-to-end and prints the honest report (matched 6382.00 / recovered 200.00 / residual 750.00, ledger balanced).

**Milestone:** RECLAIM is now a **runnable, end-to-end, deterministic engine** — detect (exact + fuzzy + gated AI) → recover (bounded, compliant) → book (double-entry) → report (honest 3 numbers + exception list) — with the LLM and payment rail as clean drop-ins behind interfaces.

**Open items:** durable/event-sourced persistence; Merkle transparency audit log; ingestion/normalisation connectors; real LLM `ExceptionResolver` + real `RecoveryExecutor`; a file/JSON batch loader + CLI args for the demo.

---

## 2026-08-25 — Phase 9: Batch loader + CLI (run on real JSON files)

**What:** `src/reclaim/batch_io.py` — the ingestion/normalization seam: `load_batch` / `load_batch_file` parse JSON into canonical `Transaction`s with **strict** validation (amounts must be *strings* → exact `Money`, never float; ISO-8601 timestamps; typed refs; domain-invariant violations surfaced as located `BatchLoadError`s). `src/reclaim/cli.py` + `__main__.py` — `python -m reclaim <batch.json> [--json]` runs the **detection-only** pipeline (no external LLM/rail) and prints a human or JSON report with the residual exception list and pending-review candidates. Shipped `examples/sample_batch.json`. ADR-0006.

**Why:** Turns the engine into a tool that runs on real settlement/bank exports, and pushes the no-float discipline to the I/O boundary (where precision is most often lost). The CLI is side-effect-free and safe to run anywhere; acting on candidates (AI/recovery) stays a deliberate, separately-wired step.

**How tested:** `tests/test_batch_io.py` — 14 tests: valid load (fees default to 0, refs parsed); **float amount rejected**; missing top-level keys; each missing required field; bad timestamp; bad amount; **negative fee surfaced as BatchLoadError**; non-string refs; no-fees/no-refs ok; non-object record; file load, missing file, bad JSON; and the shipped sample validates. `tests/test_cli.py` — 6 tests: human output; `--json` output (matched 6382, residual **2** — detection-only leaves the s2 short payment, recovered 0); clean-batch (no residual/pending branches); missing-file error (rc 2); reconciliation error (missing UTR, rc 2); pending-review reporting.

**Bug + wrong-assumption caught by the tests (honest note):** (1) a negative-fee `DomainError` initially escaped un-wrapped because `_fees` was computed *outside* the guarded block — fixed by building fees inside the try (now surfaced as `BatchLoadError`, and the branch is covered). (2) My first CLI test wrongly expected 1 residual; the CLI is detection-only, so the sample's short payment (s2) correctly *stays* a residual → 2. Corrected the test.

**Result:** ✅ **189 passed, 100% line + branch coverage** (987 statements, 248 branches, 0 partial; `__main__` shim omitted per ADR-0006). Verified live: `python -m reclaim examples/sample_batch.json` → matched 6382.00 / recovered 0 / residual 950.00 (s2 short + s4 missing), ledger balanced.

**Milestone:** RECLAIM now runs on **real JSON files from the command line**, end to end, deterministically, at 100% coverage.

**Open items:** durable/event-sourced persistence; Merkle transparency audit log; CSV adapters; real LLM + real payment executor; flags to enable simulated recovery/AI in the CLI.

---

## 2026-08-25 — Phase 10: Merkle transparency audit log (tamper-evident)

**What:** `src/reclaim/audit.py` — an append-only Merkle log following RFC 6962 / Certificate Transparency (stdlib `hashlib`/`json` only): domain-separated hashing (`leaf=SHA256(0x00||data)`, `node=SHA256(0x01||l||r)`), Merkle Tree Hash root, **O(log n) inclusion proofs** (explicit-direction, generated + verified by the same recursion), `root_at(size)` prefix roots for append-only/consistency checking, and canonical (sorted-key) event serialization. `AuditEvent` + `MerkleAuditLog`. Integrated via `pipeline.build_audit_log(report, at)` (a separate composable function — the verified pipeline is untouched) that records every decision kind (exact/fuzzy/AI/recovery/residual) in fixed order. ADR-0007.

**Why:** RECLAIM's integrity pillar — every decision (including non-actions) recorded so any edit/deletion is cryptographically detectable via the root. Dependency-light and deterministic to keep 100% coverage.

**How tested:** `tests/test_audit.py` — 24 tests: empty-log root = SHA256(""), single-leaf root, two-leaf node root (known-value checks); canonical serialization order-independence; inclusion proofs verify for **every index across sizes 1–9**; logarithmic proof length; **tamper detection** (tampered event fails; editing any past entry changes the root); verify rejects wrong root/out-of-range index; `root_at` prefix matches an independent log; range guards; append type guard; determinism. Capstone `tests/test_capstone_audit.py` — a rich run (exact + fuzzy-auto + AI + recovery + residual) builds an audit log covering all decision kinds, every entry verifies under the root, and a tampered entry is detected.

**Design note (honest):** used explicit-direction inclusion proofs rather than RFC 6962's index-derived directions — an equivalent, valid Merkle proof chosen so the verifier is transparently correct rather than subtly wrong (a wrong crypto verifier is worse than none). Full RFC 6962 O(log n) consistency proofs deferred; the O(m) prefix recompute (`root_at`) is correct and simpler for the foundation.

**Result:** ✅ `audit.py` 24 tests, **100% line + branch**. Pipeline re-tested after the additive `build_audit_log` (no regression). Full suite: **215 passed, 100% line + branch coverage** (1080 statements, 278 branches, 0 partial) across all twelve modules.

**Milestone:** every RECLAIM run can now emit a **tamper-evident, provable audit trail** of its decisions — the integrity guarantee the product/architecture promise, delivered in code.

**Open items:** durable/event-sourced persistence (the audit log + ledger are the things to persist); RFC 6962 O(log n) consistency proofs at scale; CSV adapters; real LLM + real payment executor.

---

## 2026-08-26 — Phase 11: Durable persistence (event-sourced)

**What:** `src/reclaim/persistence.py` — append-only, event-sourced storage. `EventStore` protocol with two implementations: `InMemoryStore` (copies on read/write so callers can't mutate history) and `JsonlFileStore` (durable JSON Lines; file only ever appended). (De)serializers for `Money` (`{amount:str, currency}` — never float), `LedgerEntry`, `Posting`, and `AuditEvent`. `LedgerRepository.save_posting/load` rehydrates a `Ledger` with identical balances; `AuditRepository.append_event/load` rehydrates a `MerkleAuditLog` with the **identical Merkle root**. Malformed data → located `PersistenceError`. ADR-0008.

**Why:** Ledger and audit state must survive restarts. Both are append-only and deterministically replayable, so event sourcing is the natural fit — it preserves immutability (G3) and replay determinism (G4) instead of fighting them.

**How tested:** `tests/test_persistence.py` — 17 tests: posting/event record round-trips; **Money precision preserved** through a record (95.6640 exact — at the Money level, since ledger entries must be rounded); in-memory + **durable file** ledger round-trip (a fresh repo/"process" reads the same balances); **append-only** file (earlier bytes never rewritten); deterministic reload; **audit round-trip preserves the Merkle root** and inclusion proofs still verify; corruption (bad JSON line), malformed posting/event/money records, missing top-level field, **conflicting duplicate posting id caught on replay** (`DuplicatePostingError`), empty file, blank-line skip, and in-memory history isolation.

**Wrong-assumption caught by the tests (honest note):** my first precision test tried to store an unrounded amount (95.6640) *inside a `LedgerEntry`*, which the Phase-2 invariant correctly rejects (no sub-cent dust postable). Moved the precision check to the `Money`-serialization level, where full precision is genuinely preserved. (6th such catch across the build.)

**Result:** ✅ `persistence.py` 17 tests, **100% line + branch**. Full suite: **232 passed, 100% line + branch coverage** (1171 statements, 288 branches, 0 partial) across all thirteen modules.

**Milestone:** RECLAIM state is now **durable** — the ledger and the tamper-evident audit log persist to disk (append-only) and rehydrate to byte-identical balances and Merkle root. Storage sits behind an `EventStore` interface (JSONL now; SQLite/event-store service later without domain change).

**Open items:** wire persistence into the pipeline/CLI (persist a run); snapshot/compaction for O(1) load; RFC 6962 O(log n) consistency proofs; CSV adapters; real LLM + real payment executor.

---

## 2026-08-26 — Phase 12: Wire persistence into pipeline + CLI

**What:** `pipeline.persist_run(report, at, ledger_store, audit_store) -> MerkleAuditLog` — saves a run's ledger postings and audit events through injected (append-only) `EventStore`s, returning the audit log so callers can report its root. CLI gains `--store DIR` (persists append-only `ledger.jsonl` + `audit.jsonl` under DIR, prints counts + audit root) and `--at ISO` (audit-event stamp; default = first transaction's ts via a deterministic `_run_stamp`). Reloading the stores reproduces identical balances and Merkle root. README updated. (Design covered by ADR-0008; DI stores keep it testable — InMemory in tests, JSONL on the CLI.)

**Why:** Makes a run *durable and replayable* from the entry point, not just in the library — the audit trail and books can be saved and later re-verified.

**How tested:** `tests/test_persist_run.py` — 3 tests: full round-trip (reload reproduces every account balance + the audit root, and inclusion proofs still verify), determinism (identical persisted records + root across runs), expected counts. `tests/test_cli.py` gained 5: `--store` persists files that reload to a balanced ledger + verifiable audit; `--at` override; bad `--at` errors (rc 2); banks-only batch (audit stamp fallback to bank ts); empty batch (`_run_stamp` None → epoch fallback, 0 postings/0 events).

**Result:** ✅ Full suite: **240 passed, 100% line + branch coverage** (1201 statements, 298 branches, 0 partial) across all thirteen modules. Verified live: `python -m reclaim examples/sample_batch.json --store ./runstore` wrote `ledger.jsonl` (2 postings) + `audit.jsonl` (4 events); a fresh reload reproduced the balanced ledger and the same audit root (`97b9c3599c2dc00f…`).

**Milestone:** a RECLAIM run is now **persisted and replayable end to end from the CLI** — ingest JSON → detect/recover/book → write an append-only, tamper-evident record to disk → reload to byte-identical state.

**Open items:** snapshot/compaction for O(1) load; RFC 6962 O(log n) consistency proofs; CSV adapters; a `--replay DIR` command to load & re-verify a stored run; real LLM + real payment executor.

---

## 2026-08-26 — Phase 13: Replay verification command

**What:** `src/reclaim/verification.py` — `verify_stores(ledger_store, audit_store, expect_root=None) -> VerificationResult` rehydrates a stored run and re-checks: ledger balanced per currency, every audit inclusion proof valid under the recomputed root, and (optional) the root matches a published `expect_root`. CLI gains `--replay DIR [--expect-root HEX]` (the `batch` positional is now optional); rehydration failures (corrupt JSON / broken invariants / conflicting posting id) are caught and reported as `FAILED`. Exit codes: 0 verified, 1 verification failed, 2 usage/no-run. ADR-0009.

**Why:** Completes the durability story — a stored run can be *proven* unaltered at any later time. Publish the audit root once, replay later, confirm it is unchanged; any edit changes the root and fails.

**How tested:** `tests/test_verification.py` — 5 tests: good run verifies (balanced, proofs ok, no expected root); correct expected root → match & ok; wrong expected root → mismatch & not-ok; **tampered audit store** (extra record) → root changes → fail; empty stores vacuously ok. `tests/test_cli.py` gained 5: `--replay` VERIFIED on a persisted run; `--expect-root` mismatch → FAILED (rc 1); **tampered/corrupt ledger file** → FAILED (rc 1); missing run dir → rc 2; neither batch nor `--replay` → rc 2.

**Result:** ✅ Full suite: **250 passed, 100% line + branch coverage** (1258 statements, 306 branches, 0 partial) across all fourteen modules. Verified live: persisted a run (root `2e01c7d0…`), `--replay` → VERIFIED; then appended a GHOST audit event and replayed with `--expect-root` → **FAILED, root matches: False** (root changed to `aef0ac07…`), exit 1.

**Honest note:** without `--expect-root`, verification confirms *internal* consistency (balanced + valid proofs) but cannot detect a wholesale rewrite of both files; the published-root check is the real anti-tamper guarantee (stated in ADR-0009). Record signing is a future step for authenticity.

**Milestone:** RECLAIM now closes the integrity loop end to end — **run → persist (append-only) → replay-verify (tamper-evident)** — all from the CLI, deterministically, at 100% coverage.

**Open items:** record signing (authenticity); snapshot/compaction; RFC 6962 O(log n) consistency proofs; CSV adapters; real LLM + real payment executor.

---

## 2026-08-26 — Phase 14: Record signing (HMAC authenticity)

**What:** `src/reclaim/signing.py` — HMAC-SHA256 over the audit Merkle root (stdlib `hmac`): `sign_root/verify_root`, `signed_root_record/verify_signed_record` (attestation `{algo, root, signature}`), constant-time compare, bad-signature-safe (returns False, raises only on invalid key). CLI `--key-file PATH`: on `--store` writes `audit.sig`; on `--replay` recomputes the root and verifies the signature against it (authenticity + integrity in one check). ADR-0010.

**Why:** The Merkle root proves *unaltered*; a signature proves *who attested*. Forging a signature over a modified root needs the key, so `audit.sig` can sit with the data and still be tamper-evident.

**How tested:** `tests/test_signing.py` — 13 tests: sign/verify round-trip, determinism, wrong key, tampered message, bad/non-ASCII/non-str signature → False, empty-key & non-bytes-message rejected, root sign/verify, signed-record verify + root-mismatch + wrong-key + bad-algo/shape + tampered-signature. `tests/test_cli.py` +5: sign-on-store then verify-on-replay (VERIFIED); tamper after signing → signature FAILED (rc 1); missing `audit.sig` → MISSING (rc 1); missing key file / empty key file → rc 2.

**Result:** ✅ `signing.py` 100% line + branch. Honest limitation (ADR-0010): HMAC is symmetric (verifier needs the key); Ed25519 public-key signing is the documented upgrade.

---

## 2026-08-26 — Phase 15: CSV input support

**What:** `src/reclaim/csv_io.py` — `load_batch_csv(path)` reshapes one CSV (row per record, `record_type` = settlement/bank; core + optional fee/ref columns) into the canonical dicts that `batch_io.load_batch` validates, then delegates. CLI `--csv FILE`. Shipped `examples/sample_batch.csv`. ADR-0011.

**Why:** Real data is often CSV. Reusing the JSON loader's validation means CSV inherits the amounts-as-strings (no-float) rule and identical error messages — zero duplicated logic.

**How tested:** `tests/test_csv_io.py` — 11 tests: shipped CSV loads (fees/refs parsed); **CSV == JSON equivalence** (the two shipped samples produce identical objects); blank fee/ref cells → None/no-fees; no-refs row; fee columns build fees; unknown record_type; missing required cell / missing record_type column / empty file / missing file / bad amount all raise `BatchLoadError`. `tests/test_cli.py` +2: `--csv` run matches the JSON sample's numbers; bad CSV path → rc 2.

**Result:** ✅ `csv_io.py` 100% line + branch. Full suite: **281 passed, 100% line + branch coverage** (1368 statements, 348 branches, 0 partial) across all fifteen modules. Verified live: `--csv examples/sample_batch.csv` → matched 6382.00 (same as JSON); `--store --key-file` → signed `audit.sig`; `--replay --key-file` → verification VERIFIED + signature VERIFIED.

**Bug caught (7th of the build):** the CSV sample used `2026-08-26` timestamps while the JSON sample used `2026-08-25`, so the CSV==JSON equivalence test correctly failed until the timestamps were aligned.

**Milestone:** RECLAIM now ingests **JSON or CSV**, and its stored runs carry a **signed, tamper-evident** audit trail — authenticity + integrity, verifiable offline.

**Open items:** Ed25519 public-key signing; snapshot/compaction; RFC 6962 O(log n) consistency proofs; multi-file CSV; real LLM + real payment executor.

---

## 2026-08-26 — Phase 16: LLM-backed exception resolver (first real AI integration)

**What:** `src/reclaim/llm_resolver.py` — `LLMExceptionResolver` implements the Phase-6 `ExceptionResolver` protocol by calling a `ChatClient` seam (`complete(system, user) -> str`). It builds a prompt describing the settlement/bank candidate + prior score, parses a strict JSON verdict `{is_match, confidence, rationale}` into an `Assessment` (confidence → `Decimal`, no float), and turns *any* failure into a `ResolverError` (safe degradation → the gate escalates). Real backend `build_anthropic_chat_client()` is a thin factory (lazy `import anthropic`, structured JSON via `output_config.format`, default model `claude-opus-4-8`, `# pragma: no cover`). Deterministic fakes (`Static`/`Sequence`/`Raising`ChatClient) make the logic 100%-testable offline. ADR-0012.

**Why:** Wire a real LLM into the gated resolver without coupling the core to a network SDK or breaking the coverage discipline, and without ever letting a model failure cause a wrong match — it can only cause an escalation.

**How tested:** `tests/test_llm_resolver.py` — 20 tests: valid-verdict parse; int/fenced/plain-fenced confidence; default-model constant; **every bad output → ResolverError** (non-JSON, non-object, missing field, non-bool is_match, out-of-range/non-numeric confidence, non-text, client raises, client raises ResolverError directly); prompt includes both records; and **GatedResolver integration** (LLM backend → CONFIRMED / ESCALATE-on-low-confidence / ESCALATE-on-error / REJECTED) plus a **full `run_reclaim` pipeline** driven by an LLM-backed gated resolver (fake client) confirming a garbled-UTR match.

**Result:** ✅ `llm_resolver.py` 100% line + branch. Full suite: **302 passed, 100% line + branch coverage** (1440 statements, 364 branches, 0 partial) across all sixteen modules.

**Milestone:** RECLAIM's AI layer now has a **real, drop-in LLM implementation** — contained by the gate, fail-safe by construction, and provably tested offline. The only remaining external integration is a live payment `RecoveryExecutor`.

**Open items:** live payment executor (Razorpay test-mode); Ed25519 public-key signing; snapshot/compaction; RFC 6962 consistency proofs.

---

## 2026-08-26 — Sprint 1.1: Live payment executor (Razorpay test-mode)

**What:** `src/reclaim/payments.py` — `GatewayRecoveryExecutor` implements the Phase-7 `RecoveryExecutor` protocol by charging through a `PaymentGateway` seam. `money_to_minor_units` converts `Money`→integer paise (rejects sub-unit/non-positive). The engine's idempotency key passes straight to the gateway (end-to-end no-double-charge). Gateway exception or non-`ChargeResult` → `RecoveryError` → engine HALTs. Fakes (`AlwaysPay`/`AlwaysDecline`/`Sequence`/`Raising`) for tests; thin `build_razorpay_gateway()` factory (lazy import, test-mode keys, `# pragma: no cover`). ADR-0013.

**Why:** Completes the last external integration — recovery can move real money in test-mode — while every non-network path stays 100%-tested and the core stays dependency-free.

**How tested:** `tests/test_payments.py` — 14 tests: minor-unit conversion (INR/JPY/USD; rejects sub-unit/zero/negative/non-Money); success→SUCCEEDED / decline→FAILED / non-ChargeResult→RecoveryError / gateway-raise→RecoveryError; **idempotency key + amount passthrough**; RecoveryEngine integration (recovered / exhausted / recovered-on-2nd-attempt with distinct keys / HALT-on-outage); base-gateway abstract; sequence exhaustion.

**Result:** ✅ `payments.py` 100% line + branch. Full suite: **317 passed, 100% line + branch coverage** (1496 statements, 374 branches, 0 partial) across all seventeen modules.

**Milestone (both external integrations now present):** the LLM resolver and the payment executor are both real drop-ins behind tested seams. RECLAIM can, in principle, run the whole loop against a live model and a live test-mode rail — with every deterministic path proven offline.

---

## 2026-08-26 — Sprint 1.2: Visual run dashboard

**What:** `src/reclaim/dashboard.py` — `render_report(report, audit_root=...)` produces a self-contained HTML dashboard (inline CSS, no JS, no deps) from a `RunReport`: headline cards (total expected / matched / recovered / residual), a match-rate bar, a "what happened" table (fuzzy/AI/recovery counts), the honest residual exception list, and the integrity section (ledger-balanced pill + audit root). All values HTML-escaped. `write_dashboard(...)` writes it to disk.

**Why:** The demo artifact — turns the engine's honest numbers into something a judge can see at a glance.

**How tested:** `tests/test_dashboard.py` — 5 tests: headline numbers present, audit root embedded, empty-report path, file write, escaping. `dashboard.py` 100% coverage. Generated `examples/demo_dashboard.html` (+ screenshot) from the demo run: matched 6382 / recovered 200 / residual 750 (s4), ledger balanced, audit root shown.

**Result:** ✅ Full suite: **322 passed, 100% line + branch coverage** (1517 statements, 374 branches, 0 partial) across all eighteen modules.

---

## 2026-08-28 — Maintenance: reproducible demo artifacts + cross-platform determinism

**What:** Three fixes plus a runbook, found by re-running the whole project end to end.
1. `docs/RUNBOOK.md` — a one-page reproducible demo: exact commands, verbatim expected output, the sample data explained row by row, tamper-detection walkthrough, exit-code table. Documents the `PYTHONPATH=src` / `pip install -e .` step that `python -m reclaim` needs and that both READMEs previously omitted.
2. **`examples/demo_dashboard.html` was not reproducible.** Its audit root was `8b1bfada…`, but regenerating from the code yields `396f405b…`. Cause: the shipped artifact was stamped with the build date (`2026-08-26T09:00`) instead of the batch's own deterministic `TS` (`2026-08-25T09:00`) — the same off-by-one-day that the JSON/CSV sample files hit earlier. Regenerated both the HTML and the PNG from `TS`.
3. **Generated files used platform-dependent line endings.** `dashboard.write_dashboard` and `persistence.JsonlFileStore.append` opened files in text mode without `newline=`, so Python emitted CRLF on Windows and LF on POSIX for byte-identical runs. Fixed both to `newline="\n"`.

**Why:** The repo's headline claim is that runs are deterministic and replayable — "same inputs produce the same audit root". A flagship artifact nobody could regenerate contradicted that, and store files whose bytes depend on the OS weaken an append-only tamper-evident log (the Merkle root was never affected — it is computed over record content, not file bytes — so this was a reproducibility defect, not a correctness one).

**How tested:** Full suite re-run: **322 passed, 100% line + branch** (1517 statements, 374 branches, 0 partial). Every RUNBOOK step scripted and asserted on exit code and output: demo, JSON, CSV, JSON≡CSV byte-equality, `--store` root `2e01c7d0…`, `--replay --expect-root` (exit 0), tampered ledger (exit 1), wrong root (exit 1), usage error (exit 2). Dashboard regeneration confirmed byte-identical on repeat runs and now writes pure LF (0 CRLF). Store files verified LF-only; `--replay` still verifies with the audit root unchanged at `2e01c7d0…`. Verified `pip install -e ".[dev]"` in a clean venv: `python -m reclaim` and the suite both run with no `PYTHONPATH`. Both README mermaid diagrams rendered with mermaid-cli to confirm they parse.

**Result:** ✅ Demo artifacts are now reproducible from source on any platform; docs match the code.

**Open items:** `domain._validate_confidence` accepts `int`/`float` and **rejects `Decimal`**, while `resolver.Assessment` requires `Decimal` and rejects `float` — the two confidence validators are exactly inverted. Not a live bug: no engine path populates `Transaction.match_confidence` or `LeakRecord.confidence` (only tests do), so the fields are unused API surface. It is a trap for whoever first threads the probabilistic matcher's `Decimal` score into `match_confidence`. Decide: converge on `Decimal` everywhere, or drop the two unused fields.

---

## 2026-08-28 — Post-Sprint-1 adversarial review: three integrity fixes

**What:** An adversarial read of the finished engine found three defects that all share one shape —
**the system reporting success it had not earned.** All three are fixed, each with an ADR and a
regression test that fails on the old behaviour.

1. **No closure number existed.** `match_rate` = `matched / total_expected`, and `matched` summed
   reconciled settlements only. The demo recovered ₹200 and the reported rate did not move — for a
   product whose thesis is "close the loop and prove it", nothing in the output changed when the
   loop closed. Added `closed_amount()` and `closure_rate()` **alongside** `match_rate` rather than
   redefining it, so the before/after pair stays visible: detection `0.6299` → closure `0.6496`.
   Surfaced in `summary()`, the CLI, `reclaim.demo`, and the dashboard. ADR-0014.
2. **Unanchored replay reported VERIFIED on a gutted audit log.** `verify_stores()` recomputes the
   Merkle root from the stored events and checks inclusion proofs against *that* root — a
   self-referential check. Deleting 2 of 4 audit events and replaying printed `VERIFIED`,
   `inclusion proofs: ok`, **exit 0**. `--store` now publishes `root.txt`; `--replay` resolves an
   anchor (`--expect-root` → `root.txt` → `--key-file` signature) and **refuses with exit 2** when
   there is none, instead of issuing a verdict it cannot justify. The output names its anchor.
   ADR-0015.
3. **Persisting a run twice was not idempotent.** `AuditRepository.append_event` appended
   unconditionally, so a second `persist_run()` doubled the events (4 → 8) and changed the reloaded
   root — a legitimate re-run became indistinguishable from tampering. Both repositories now share
   the content-idempotency rule `Ledger.post` already had. ADR-0016.

Two further review findings were **documentation** problems, not code defects, and are fixed as
such: the RBI 24-hour notice is *modelled* (time recorded, attempts scheduled after it) but never
sent — there is no notice executor; and demo recovery outcomes come from `AlwaysSucceedsExecutor`
with no control group or causal uplift. Both are now stated plainly in an honest-limits table in
the README and the RUNBOOK, and the product document's worked example is labelled an illustrative
target scenario with the engine's actual measured numbers beside it.

**Why:** A verifier that passes on tampered data is worse than no verifier — it converts absence of
evidence into a green tick, and a scripted CI check would never notice. A closure engine that
cannot show closure has no output for its own thesis. And an honest limit stated late reads as a
concession; stated first, it reads as rigour.

**How tested:** 14 new tests. `test_cli.py` — root.txt publication, auto-anchoring, the deleted-events
regression (asserts the gutted log still self-verifies, which is *why* the anchor is required),
unanchored refusal, empty root.txt, explicit override, key-only anchoring both clean and tampered,
closure in the JSON summary. `test_persistence.py` — audit/ledger append idempotency, dedupe against
a pre-existing store, distinct events preserved. `test_persist_run.py` — double persist leaves files,
event count and root unchanged and replay still verifies. `test_dashboard.py` — closure story
rendered, meter segments clamped. Full suite re-run: **338 passed, 100% line + branch** (1557
statements, 390 branches, 0 partial). Every RUNBOOK step re-verified end to end against the new
behaviour, including all three tamper modes and the idempotent re-persist.

**Result:** ✅ Detection→closure is now visible in every output; a replay either proves something or
refuses; a re-run can never masquerade as tampering. 19 modules, 338 tests, ADRs 0001-0016.

**Open items:**
- `domain._validate_confidence` accepts `int`/`float` and **rejects `Decimal`**, while
  `resolver.Assessment` requires `Decimal` and rejects `float` — inverted validators. Not a live bug
  (no engine path populates `Transaction.match_confidence` or `LeakRecord.confidence`), but a trap
  for whoever first threads the probabilistic `Decimal` score into it. Converge on `Decimal` or drop
  the two unused fields.
- The 24-hour notice has no executor. If it should be an *action* rather than a recorded time, it
  needs a `NoticeExecutor` seam mirroring `RecoveryExecutor` — new module, tests, ADR.
- No causal measurement. Control-group assignment and uplift are Sprint 2 and are currently claimed
  nowhere.
- **Environment hazard (not a repo defect):** a global editable install at
  `site-packages/__editable__.reclaim_engine-0.0.1.pth` points at a *different* checkout
  (`Downloads/Razorpay_Testing/RECLAIM/reclaim-engine/src`). `pytest` is unaffected (`pyproject.toml`
  sets `pythonpath = ["src"]`, which wins), but a bare `import reclaim` from a directory without
  `src` on the path silently loads the other copy. The RUNBOOK now includes a one-line check.

---

## 2026-08-28 — Sprint 2: six phases toward the full architecture (18–23)

**What:** Closed the largest gaps between the engine and
`RECLAIM-System-Architecture.md`, taking implemented components from roughly a
third to roughly a half, and satisfying goal **G9** for the first time. Each
phase extended a module that already existed — nothing structural was rewritten.

- **Phase 18 · Blocking** (§6 stage 1) — candidate generation derived from the
  scoring config rather than guessed, so it is *provably* lossless: a pair whose
  amount score is below `(T - w_d - w_r) / w_a` cannot clear the review
  threshold whatever else agrees. 2000×2000 drops from 4,000,000 scorings to
  63,688 (1.6%) in 0.27s. Disables itself when no sound block exists. ADR-0017.
- **Phase 19 · Leak Ledger** (§5.2) — the seam between reconciliation and
  recovery, which was a type with no store. Append-only and *versioned*: state
  changes append a version, never edit, so `history()` answers "why is this leak
  in this state?". New `RecoveryState.SUPERSEDED` for leaks a later fuzzy/AI
  match resolved — they were never missing money and must not sit in a queue.
  `evidence[]` and a real `audit_ref` (`"<root>:<leaf index>"`, resolvable to an
  inclusion proof) complete Appendix B. ADR-0018.
- **Phase 20 · Consistency proofs** (§9.1) — RFC 6962's other half. `--store`
  appends each published head to `roots.log`; `--replay` proves the log is an
  append-only extension of every head ever published. This catches the attack
  ADR-0015 could not: rewrite history *and* refresh `root.txt`, and inclusion
  still passes while append-only fails. ADR-0019.
- **Phase 21 · Executed notice + AFA ceiling** (§7, §10) — the RBI 24-hour
  notice becomes an action behind a `NoticeExecutor` seam; a rejected or failed
  notice halts with zero attempts (no debit without notice). `afa_limit`
  (₹15,000) halts autonomous debits that would need customer authentication.
  ADR-0020.
- **Phase 22 · Causal measurement** (§9.2, **G9**) — deterministic hash-based
  holdout, and lift measured as treated minus control. Reports `None` rather
  than a flattering 100% when there is no control cohort; flags underpowered
  cohorts; reports negative lift as negative. Worked example: gross ₹6,000,
  causal ₹3,500. ADR-0021.
- **Phase 23 · Scorecard** (§9.3) — the five-part ungameable scorecard: match
  and closure rates, gross *and* causal recovery, residual, conduct
  (`contacts_per_unit`, `wasted_contact_rate`, `notice_compliance`,
  `halted_for_human`) and `time_to_closure_hours`. ADR-0021.

**Why:** The prior gap analysis ranked blocking, a persisted Leak Ledger and
causal measurement as the three most load-bearing absences — the first is a hard
ceiling on batch size, the second is the contract two halves of the system are
supposed to meet across, and the third is a stated goal with nothing behind it.
Consistency proofs and the notice executor closed claims the code was making
without honouring.

**How tested:** 34 new tests; full suite **420 passed, 100% line + branch**
(2025 statements, 554 branches, 0 partial) across 22 modules. Highlights, chosen
because they test the property rather than the code path:
`test_blocking_is_lossless_against_brute_force` runs a seeded corpus of
tolerance-edge near-misses through the blocked matcher and an independent
brute-force oracle and demands identical results; `test_consistency_proof_over_
every_prefix_pair` verifies all 561 `(m, n)` pairs up to 33 entries, and
truncating a proof at *any* position is rejected;
`test_inclusion_alone_cannot_catch_that_rewrite` asserts the gap consistency
proofs exist to fill; `test_open_queue_equals_the_reports_honest_residual` pins
two representations of "what a human owns" that could otherwise drift;
`test_a_worthless_intervention_measures_as_worthless` and
`test_no_control_group_reports_none_not_a_perfect_score` are the measurement
module's own honesty checks.

**Bugs this work surfaced and fixed:** `root.txt` and the HMAC signature were
written from the *run's* audit log rather than the *store's* — identical for a
fresh directory, wrong the moment a second batch is persisted into the same one.
Found by the first multi-run consistency test, not by review.

**Result:** ✅ 22 modules, 420 tests, 100% line + branch, ADRs 0001–0021.
Architecture coverage by layer: Data Platform ~10% (unchanged), Ledger Core
~85%, Reconciliation ~80%, Leak Classifier ~90%, Recovery ~50%, Agentic Control
~15%, Integrity & Measurement ~85%, cross-cutting ~35%. Goals: **G1–G9 met**
(G9 newly), G10 outstanding.

**Open items:**
- **G10 scale** — blocking removes the matching ceiling, but the engine is still
  single-process and batch-only. No Kafka, no CDC, no medallion layers.
- **Layer 6 agentic control** remains the thinnest: escalation is an outcome,
  not a runtime state. Nothing can pause, persist and wait for a human.
- **Recovery ML** — uplift model, funded-moment predictor and contextual bandit
  are still absent; the bandit is also the prerequisite for the IPS/DR offline
  evaluation and drift monitors in §9.2.
- **Control outcomes are not yet observed.** The holdout assigns cohorts and the
  measurement is correct, but crediting real lift needs the T+1 re-reconciliation
  loop to observe whether held-out leaks self-resolved. Deliberately not faked.
- Cross-run contact caps and consent state are now *possible* (the Leak Ledger
  persists) but not implemented.
- `domain._validate_confidence` still rejects `Decimal` while
  `resolver.Assessment` rejects `float` — carried over, still unresolved.

---

## 2026-08-31 — Sprint 3, Phase 24: the T+1 observation loop

**What:** `src/reclaim/observation.py` and a runnable `src/reclaim/experiment.py`
(`python -m reclaim.experiment`). Sprint 2 left causal measurement assigned but
never scored: `causal_recovered` was structurally unreachable. This phase
re-reconciles a **follow-up batch** and reads each unit's outcome out of the
bank data.

- **Only observed resolution counts.** A unit is recovered when the T+1 run no
  longer lists its leak as residual. `RecoveryOutcome.RECOVERED` — which means
  "the executor returned success", a fact about an API call — is deliberately
  *not* used as an outcome for either arm, because scoring treated units on the
  engine's claim while scoring control units on bank data is asymmetric evidence
  that biases flattering every time.
- **The claim becomes a check on itself.** Engine said `RECOVERED`, follow-up
  batch disagrees → the unit lands in `claimed_not_observed`. In the shipped
  demo that fires for **42 of 135** treated units.
- **Intention-to-treat.** A treated unit that halted, hit the AFA ceiling or
  exhausted its attempts stays in the treated cohort as not-recovered. Dropping
  it would measure "how well recovery works when it runs" — improvable by
  refusing to run.
- **Unobserved ≠ resolved.** A unit whose settlement is absent from the
  follow-up batch is reported with a reason code, never counted as a win.
  Superseded leaks are excluded from both cohorts; they were never real money.
- Arms are read from what the run actually did (`report.control_leaks`), not
  re-derived from a policy object that could disagree with the run.

ADR-0022.

**Why:** It was the single largest honest-limit in the project — the one line in
the runbook that said "not yet produced". It is also the gap that a buyer would
find first: every recovery vendor reports gross, and the difference between
gross and causal is the entire product argument.

**How tested:** 45 new tests; full suite **465 passed, 100% line + branch**
(2183 statements, 594 branches, 0 partial) across 24 modules. The tests worth
naming are the ones that try to make the module lie:
`test_resolution_is_read_from_the_followup_not_from_the_engine` and
`test_full_loop_catches_a_recovery_the_bank_data_never_confirms` (a claimed
recovery with no bank evidence must score as not-recovered *and* be flagged);
`test_a_unit_not_carried_forward_is_unobserved_not_recovered` ("we stopped
looking" must not collapse into "the money arrived");
`test_removing_the_control_group_reports_none_not_a_better_number`;
`test_outcome_draw_is_independent_of_cohort_assignment` (a shared salt between
holdout and fixture would manufacture a lift out of correlated hashes); and
`test_claimed_exceeds_observed_exceeds_causal`, which pins the ordering of the
three figures the loop exists to separate.

**Result:** ✅ the demo now prints a *measured* lift. On the shipped fixture:
**27.35 pp** lift, treated 93/135 vs control 27/65, and the three recovery
numbers in descending order of honesty — **₹27,000 claimed → ₹18,600 observed →
₹7,384.20 causal**, not underpowered. `notice_compliance` reaches `1.0000` for
the first time (the experiment wires a `NoticeExecutor`).

**Open items / honest notes:**
- The two batches are **synthetic** and the T+1 fixture's rates are authored
  (65% treated / 40% control), so the printed lift is a property of the fixture.
  The machinery is real; no lift has been measured on production data.
- No observation *window* is modelled. "By T+1" means "in the batch you handed
  me"; a real deployment needs a stated window and a rule for units still in
  flight at its close.
- Nothing yet feeds the observed outcomes back as training signal — that is what
  the uplift model and bandit will consume.

---

## 2026-08-31 — Sprint 3, Phase 25: the HITL control plane (Layer 6)

**What:** `src/reclaim/control.py`, a `GateRepository` in `persistence.py`, and
`--queue` / `--decide` on the CLI. Layer 6 was the engine's thinnest layer
(~15%) for a specific reason: `ESCALATE_HUMAN` and `HALTED` were terminal labels
on a report that then exited the process. There was a human in the name of the
design and no loop for them to be in.

    open a typed gate  ->  AWAITING_HUMAN  ->  APPROVED / REJECTED / CANCELLED
         (checkpoint)         (durable)              (resumable)

- **Gates are typed and *derived*, not hand-raised.** `gates_for_run` reads a run
  and produces every question it owes a human: one per AI escalation, one per
  halted recovery, and a `VALUE_THRESHOLD` sign-off for chasing a large leak even
  when nothing went wrong. A halt at the AFA ceiling gets its own kind, because
  "has the customer authenticated?" and "should we keep going?" are not the same
  question. Deriving means a gate cannot be forgotten.
- **A gate is decided exactly once.** Re-deciding raises; replaying the identical
  decision is a no-op. Two approvals of one high-value debit is the failure mode
  that costs a merchant money, so the state machine prevents it rather than a UI
  that hides the button.
- **Approval authorises; it does not act.** `resume` executes through the same
  injected engine with the same notice window, AFA ceiling and idempotency keys.
  A human "yes" on an over-ceiling debit still halts — approval changes who is
  accountable, not what is safe.
- **Rehydration cannot invent consent.** A gate log whose first record for an id
  is already `approved` is refused: a decision with no open checkpoint behind it
  means the log lost the question.
- **No anonymous, undated decisions.** `--decide` requires `--actor` and an
  explicit `--at`. The engine never reads the clock (G4).

ADR-0023.

**Why:** It was both the largest architecture gap and the one that makes the rest
usable. Every safety mechanism already built — the resolver's escalation, the
recovery halt, the AFA ceiling — routed to a human who had no queue, so in
practice all three degraded to "printed and forgotten".

**How tested:** 67 new tests; full suite **530 passed, 100% line + branch**
(2507 statements, 716 branches, 0 partial) across 25 modules. The load-bearing
ones try to break a safety claim: `test_a_gate_is_decided_exactly_once`,
`test_approving_a_gate_moves_no_money`,
`test_a_human_yes_does_not_bypass_the_afa_ceiling`,
`test_an_unanswered_gate_makes_the_resume_incomplete`,
`test_a_log_whose_gate_opens_already_approved_is_refused`, and
`test_the_checkpointer_survives_a_restart_on_disk` — write a gate in one process,
load it in another, answer it, reload and find the decision and its history
intact.

**Result:** ✅ the loop is drivable from the CLI end to end:

```
python -m reclaim batch.json --store ./run --value-threshold 15000
  -> human queue: 1 gate/s awaiting -> run/gates.jsonl
python -m reclaim --queue ./run
  -> [value_threshold] gate:value:leak:short:s5  20000.00 INR
     Sign off chasing 20000.00 INR, above the 15000.00 INR threshold?
     parked behind unanswered questions: 20000.00 INR
python -m reclaim --queue ./run --decide gate:value:leak:short:s5 \
    --verdict approve --actor ops@example.com --at 2026-08-26T11:15:00
  -> approved: gate:value:leak:short:s5 by ops@example.com
```

**Open items / honest notes:**
- **Not a workflow engine.** No scheduler, no timer, no retry-on-crash, no SLA on
  how long a gate may sit. Temporal and a LangGraph checkpointer own those; what
  is built is the durable state and transition rules they would persist.
- `resume` does not re-enter the pipeline. An approved match gate is *reported*
  as confirmed rather than folded back into a fresh `RunReport` with a recomputed
  match rate; closing that circle means re-running reconciliation with human
  overrides as an input.
- `Gate.audit_ref` exists and persists but nothing populates it, so a decision is
  not yet provable against the Merkle root the way a leak is.
- Cross-run contact caps and consent state remain unimplemented.

---

## 2026-08-31 — Sprint 3, Phase 26: the recovery ML layer (uplift + funded moment)

**What:** `src/reclaim/uplift.py` and `src/reclaim/timing.py`, wired into the
loop via an optional `Targeting` on `run_reclaim` and an `AttemptScheduler` seam
on `RecoveryEngine`. Layer 5 chased every recoverable leak on a fixed schedule;
both halves of that are now decisions rather than defaults.

**Uplift — who to chase.** A T-learner over discrete cells
(`failure reason × amount band × recency × prior failures`). Chasing a *sure
thing* inflates gross recovery while adding nothing — exactly the number ADR-0022
exists to stop us reporting — and chasing a *sleeping dog* destroys value while
looking like activity. A response model cannot tell the four segments apart
because three of them contain recoveries; only the difference between arms can.

- Trained on **observed** outcomes (the Phase 24 verdict), never the executor's
  claim. Training on claims would teach the model that whatever the executor
  reports success for is persuadable, which is not a fact about customers.
- Hierarchical fallback: cell → failure-reason pool → global →
  `INSUFFICIENT_EVIDENCE`, with the answering level named. Support required in
  *both* arms. This answers the architecture's own open question about
  small-merchant calibration in code.
- Classification order is deliberate: sleeping dogs first, so a harmed segment is
  never downgraded to merely unprofitable.
- `UnknownPolicy` defaults to CHASE. A model quietly shrinking coverage on thin
  evidence loses a merchant money they were owed.
- `skipped_leaks` carries every skip *with its reason* and `skipped_amount()`
  totals the value left on the table. A targeting layer that hides that number is
  unauditable — and a skipped leak stays on the honest residual list.
- The holdout is decided **before** targeting, so a control unit targeting would
  also have skipped still counts as held out; otherwise the experiment stops
  measuring the policy that was deployed.

**Funded moment — when to chase.** A concentration model over day-of-month and
hour: take the mode, report how concentrated the history was as the confidence.
Explains itself in one sentence ("you get paid on the 2nd") and needs a dozen
observations, not a billion. Diffuse history produces a low confidence and a note
recommending the fixed schedule.

The load-bearing decision is that the scheduler is **advisory**. The engine hands
it the notice deadline and anything returned at or before that deadline halts the
recovery rather than being obeyed. A timing model that could shorten the RBI
notice window would be a compliance bug wearing an ML hat, so the refusal lives
in the engine, not in the model's good behaviour. Only the first attempt moves;
later attempts space from wherever it landed, so a re-timed sequence does not
bunch three attempts into a day.

ADR-0024, ADR-0025.

**How tested:** 76 new tests; full suite **606 passed, 100% line + branch**
(2934 statements, 866 branches, 0 partial) across 29 modules. The ones carrying
the argument: `test_a_sure_thing_is_not_chased_despite_a_high_treated_rate`
(90% recover when chased, 85% when left alone — a response model ranks this top,
uplift declines), `test_a_sleeping_dog_outranks_a_sure_thing_label`,
`test_a_real_but_tiny_uplift_is_not_rounded_up_to_persuadable`,
`test_a_one_armed_cell_never_produces_an_uplift`,
`test_a_scheduler_cannot_shorten_the_notice_window` (three illegal proposals,
all halted with zero attempts), `test_a_retimed_attempt_never_lands_inside_the_
notice_window` (28 deadlines swept), `test_the_mode_wins_not_the_average` (the
average of the 1st and the 29th is the 15th, when the account is empty on the
15th), and `test_the_control_arm_is_decided_before_targeting`.

**Dead code removed by the discipline (honest note):** `CellStats.merge` was
written, then found to have no caller — the pooling in `fit` accumulates with
`plus` directly. Deleted rather than kept alive by a test that existed only to
cover it, the same lesson as the Phase 3 unreachable guard.

**Open items / honest notes:**
- **Nothing sources funded moments.** The predictor is fitted from a
  caller-supplied history; the per-customer credit events that would populate it
  are not associated with a customer identity anywhere in the domain. Seam and
  model are real and tested; the feed is not built.
- `Context.prior_failures` is likewise unsourced — required rather than defaulted
  so the gap is visible at every call site instead of silently reading zero.
- **No propensity logging and no offline policy evaluation**, so a *new*
  targeting policy cannot yet be validated against logged data before it ships.
  That is the bandit's prerequisite and belongs with it.
- Nothing retrains automatically, which is the right default for a model that
  decides whether to debit someone.
- The models are not yet *demonstrated* end to end — a learn-then-target second
  cycle is the next piece of work, and until it exists this phase is "built but
  not shown", which is the same criticism Sprint 2 earned for measurement.

---
