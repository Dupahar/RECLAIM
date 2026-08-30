# RECLAIM — Demo Runbook

Reproduce the full loop in ~3 minutes. **No install, no network, no API keys, no dependencies**
beyond Python 3.11+ (`pytest` only for step 1). Every output below is verbatim from a real run,
with paths shown POSIX-style (Windows prints `run\root.txt`) and key-dependent signatures elided.

> **Scope of this demo.** Detection, booking, audit, replay-verification, the HITL gates, the
> causal-measurement loop and uplift targeting are all real mechanisms. **Recovery outcomes are a
> deterministic simulation** — every demo uses a stand-in executor behind the tested
> `PaymentGateway` seam, so RECLAIM still moves no real money and dispatches no real notice. What
> that means for the numbers in steps 10 and 11: the *machinery* that measures lift and targets on
> it is real and adversarially tested, and the *batches it measures* are synthetic fixtures whose
> outcome rates are authored. No lift has been measured on production data. See
> [Honest limits](#honest-limits).

## 0. Setup

```bash
cd reclaim-engine
```

The package lives under `src/` (src layout). Either put it on the path for the session, or install it:

| Shell | Command |
|---|---|
| PowerShell | `$env:PYTHONPATH = "src"` |
| bash / zsh | `export PYTHONPATH=src` |
| or install once | `pip install -e ".[dev]"` — then `PYTHONPATH` is not needed |

`python -m pytest` works either way (`pyproject.toml` sets `pythonpath = ["src"]`).

If you have another checkout of this project installed elsewhere, confirm you are running *this*
one — an editable install on the system path silently wins over a bare `import reclaim`:

```bash
python -c "import reclaim; print(reclaim.__file__)"
```

## Sample data

`examples/sample_batch.json` and `examples/sample_batch.csv` are the **same batch in two formats**
(a test asserts they parse to identical objects). Four settlements vs three bank credits, chosen so
one row exercises each path:

| Row | Settlement | Bank credit | Exercises |
|---|---|---|---|
| s1 | ₹5000.00 gross, fees ₹118 (MDR+GST) | b1 ₹4882.00, `UTR-1` | **Exact match** after fee decomposition |
| s2 | ₹3000.00, `UTR-2` | b2 ₹2800.00, `UTR-2` | **Short payment** — ₹200 recoverable leak |
| s3 | ₹1500.00, `UTR-3`/`ORD-3` | b3 ₹1500.00, `UTR-3X`/`ORD-3` | **Garbled UTR** — probabilistic auto-match |
| s4 | ₹750.00, `UTR-4` | *(none)* | **Missing payout** — honest residual leak |

Amounts are **strings** in the input — they become exact `Decimal` `Money`, never float.

## 1. Tests — 830 passing, 100% line + branch

```bash
python -m pytest --cov=reclaim --cov-branch
```
→ `TOTAL 4062 statements, 1242 branches, 0 missed, 100%` · `830 passed`

## 2. Full closed loop (demo data, deterministic stand-ins for LLM + payment rail)

```bash
python -m reclaim.demo
```
```
  total expected : 10132.00 INR
  matched        : 6382.00 INR   (match rate 0.6299)
  recovered      : 200.00 INR
  closed         : 6582.00 INR   (closure rate 0.6496)
  residual       : 750.00 INR  (1 leak/s)
```
**This is the closure claim, and it is the whole point:** detection alone ties out 62.99%; recovery
takes it to 64.96%; the remaining ₹750 is named, not hidden. `match_rate` is deliberately the
*before* number — see [ADR-0014](decisions/ADR-0014-closure-rate.md).

## 3. Reconcile a canonical batch file (detection-only — safe, no side effects)

```bash
python -m reclaim examples/sample_batch.json           # human-readable
python -m reclaim examples/sample_batch.json --json    # machine-readable
python -m reclaim --csv examples/sample_batch.csv      # same batch, CSV input
```
→ `"matched": "6382.00 INR"`, `"match_rate": "0.6299"`, `"closed": "6382.00 INR"`,
`"closure_rate": "0.6299"`, `"residual": "950.00 INR"`, `"residual_leaks": 2`, `"auto_matched": 1`.

Recovery is `0` here — and closure therefore equals detection — **by design**: the CLI runs
detection only, so the short payment on s2 stays on the honest exception list instead of being
recovered. JSON and CSV produce identical output.

## 3b. Ingest a real-shaped delivery through the medallion layer

`--pg-csv` and `--bank-csv` go through Bronze → Silver → Gold instead of the
canonical loader: fees unpacked, references normalised, UTRs extracted from bank
narration, and **every row that cannot be conformed reported rather than dropped**
(architecture §4, [ADR-0030](decisions/ADR-0030-medallion-ingestion.md)).

```bash
python -m reclaim --pg-csv examples/pg_settlement.csv                   --bank-csv examples/bank_statement.csv
```
```
ingest: 11 row/s landed
  conformed  : 4 settlement/s, 3 bank credit/s
  quarantined: 4
    - [adapter_rejected] pg_settlement.csv line 5
        pg_settlement.csv line 5: 'settlement_id' is required
    - [adapter_rejected] pg_settlement.csv line 6
        pg_settlement.csv line 6: 'gross_amount' is not an amount (could not parse amount 'not-a-number')
    - [no_reference_found] bank_statement.csv line 4
        bank_statement.csv line 4: no recognised reference pattern in narration 'CASH DEPOSIT BRANCH 4471'
    - [adapter_rejected] bank_statement.csv line 5
        bank_statement.csv line 5: direction is 'debit'; only credits are settlement payouts, and treating a debit as one would invent money
  every landed row accounted for: True
```
→ then the usual report: `matched 6346.60 INR (match rate 0.6286)`, residual
`950.00 INR` over 2 leaks.

Three things to look at:

**`every landed row accounted for: True`** is goal G2 as arithmetic. Every landed
row is in exactly one of conformed or quarantined, and a test asserts the counts
add up — a row cannot be silently dropped.

**The reasons, not just the counts.** Each quarantined row keeps its file, line
number and a reason aimed at whoever has to fix the file. A debit is refused with
*"treating a debit as one would invent money"* rather than "invalid direction".

**The match only happens because references are normalised.** The settlement file
writes `UTR-000123456790`; the statement writes it inside
`UPI/UTR000123456790/SETTLEMENT/ACME`. Same reference, two spellings. Without
normalisation the exact matcher finds nothing and the batch reports **0% matched
with no error raised anywhere** — both files being individually valid. That
happened on the first end-to-end run, and it is the failure a validating boundary
structurally cannot catch.

> A low-confidence UTR extraction is **quarantined, not guessed**: a labelled
> `UTR…` scores 1.0, a bare run after a `NEFT`/`IMPS`/`UPI` prefix 0.75 (the same
> shape appears in account numbers), anything else 0. A wrong reference here does
> not fail loudly — it silently creates a false match.

## 4. Visual run report (HTML dashboard)

```bash
python -c "from reclaim.demo import run_demo, TS; from reclaim.pipeline import build_audit_log; from reclaim.dashboard import write_dashboard; r = run_demo(); write_dashboard(r, 'examples/demo_dashboard.html', audit_root=build_audit_log(r, TS).root())"
```
Open `examples/demo_dashboard.html`. Fully deterministic: this **rewrites the shipped file
byte-for-byte**, audit root `396f405b746787e7ac858b8f53a081f4cdb944d446b813a75b258da2fa8c3b8a`
every time. `git diff` stays empty — that is the reproducibility claim, checkable in one command.

## 5. Persist the run + publish and sign the audit root

```bash
python -c "import os, pathlib; pathlib.Path('demo.key').write_bytes(os.urandom(32))"
python -m reclaim examples/sample_batch.json --store ./run --key-file demo.key
```
```
persisted: 2 postings, 4 audit events -> run  (store now 4 events, root 2e01c7d00ad76c12...)
leak ledger: 4 leaks, 2 still open -> run/leaks.jsonl
published: run/root.txt (anchors --replay), head appended to roots.log
signed: audit.sig written (HMAC-SHA256, sig ...)
```

Writes append-only `run/ledger.jsonl`, `run/audit.jsonl` and `run/leaks.jsonl` (the Leak Ledger),
plus `run/root.txt`, `run/roots.log` (the published head history) and `run/audit.sig`.
The root is **deterministic** — the same batch (JSON *or* CSV) always yields
`2e01c7d00ad76c124ad0d906a78caf5f6b524aa8aacb4ea0fb70bb56027482f6`. Re-running this command against
the same directory is a **no-op**: nothing is duplicated and the root does not move
([ADR-0016](decisions/ADR-0016-idempotent-persistence.md)). (`run/`, `*.jsonl` and `*.key` are
gitignored.)

## 6. Replay-verify the stored run

```bash
python -m reclaim --replay ./run --key-file demo.key
```
```
replay verification: VERIFIED
  source           : run
  anchor           : run/root.txt
  postings         : 2
  ledger balanced  : True (INR)
  audit events     : 4
  audit root       : 2e01c7d00ad76c124ad0d906a78caf5f6b524aa8aacb4ea0fb70bb56027482f6
  inclusion proofs : ok
  root matches     : True
  append-only      : ok (vs 1 published head/s)
  signature        : VERIFIED
```
**exit 0.** Note the `anchor` line: replay always states what it checked against. A replay with no
anchor proves nothing and is refused — see step 7.

## 7. Tamper checks — the money shot

**7a. Corrupt a stored amount** (breaks the double-entry invariant on rehydration):

```bash
python -c "import pathlib; p = pathlib.Path('run/ledger.jsonl'); p.write_text(p.read_text(encoding='utf-8').replace('4882.00','9999.00',1), encoding='utf-8', newline='\n')"
python -m reclaim --replay ./run --key-file demo.key
```
→ `replay verification: FAILED` · `error: posting 's1' unbalanced: debits 10117.00 INR != credits
5000.00 INR` · **exit 1**

**7b. Delete audit events** — the subtle one. A Merkle log re-roots itself, so the survivors still
produce valid inclusion proofs. Only the anchor catches it:

```bash
python -c "import pathlib; p = pathlib.Path('run/audit.jsonl'); L = p.read_text(encoding='utf-8').splitlines(); p.write_text('\n'.join(L[:2]) + '\n', encoding='utf-8', newline='\n')"
python -m reclaim --replay ./run
```
→ `audit events: 2` · `inclusion proofs: ok` ← *the gutted log is internally consistent*
→ `anchor: run/root.txt` · `root matches: False` · `append-only: FAILED (vs 1 published head/s)`
· `replay verification: FAILED` · **exit 1**

**7c. Remove the anchor** — verification refuses rather than reporting a verdict it cannot justify:

```bash
rm run/root.txt && python -m reclaim --replay ./run
```
→ `error: UNANCHORED replay - tampering cannot be detected.` · **exit 2**

**Exit codes:** `0` verified · `1` verification failed (unbalanced / bad proof / root mismatch /
append-only violation / bad signature / corrupt data) · `2` usage error, including an unanchored
replay.

> `root.txt` is a *convenience* anchor: an attacker who can rewrite `audit.jsonl` can rewrite it too.
> For real tamper evidence, keep the root somewhere the run directory's owner cannot silently edit —
> commit it, file it on a ticket, or use `--key-file` and hold the key elsewhere
> ([ADR-0015](decisions/ADR-0015-anchored-replay.md)).

## 8. The scorecard and the Leak Ledger

```bash
python -c "from reclaim.demo import run_demo; from reclaim.scorecard import build_scorecard; import json; print(json.dumps(build_scorecard(run_demo()).summary(), indent=2))"
```
Note two fields that stay honest by default: `causal_recovered` is `null` with the note *"no holdout
was run, so recovery cannot be credited causally"*, and `notice_compliance` is `0.0000` because this
demo supplies no `NoticeExecutor`. Both become real numbers only when the machinery behind them is
actually used — steps 10 and 11 supply a holdout and a notice channel, and there the same two fields
read `7384.20 INR` and `1.0000`. That contrast is the point of
[ADR-0021](decisions/ADR-0021-causal-measurement.md): the metric reports `null` until something
earns it.

The Leak Ledger persisted in step 5 is the human queue:

```bash
python -c "from reclaim.demo import run_demo; from reclaim.pipeline import build_leak_ledger; ll = build_leak_ledger(run_demo()); print('open:', [l.id for l in ll.open_queue()]); print('states:', {l.id: l.recovery_state.value for l in ll.leaks()})"
```
Four leaks were detected; one was recovered, two were `superseded` by a later fuzzy match (never
real leaks), and exactly one is open — the same single leak the report calls residual.

## 9. The human exception queue — pause, persist, resume

A run can now stop and ask. `--value-threshold` opens a HITL sign-off gate for
any recoverable leak above the amount; escalations and halted recoveries open
gates automatically (architecture §8, [ADR-0023](decisions/ADR-0023-hitl-control-plane.md)).

```bash
python -c "import json, pathlib; pathlib.Path('big.json').write_text(json.dumps({'settlements': [{'id': 's5', 'gross_amount': '50000.00', 'currency': 'INR', 'ts': '2026-08-25T09:00:00', 'refs': {'utr': 'UTR-5'}}], 'bank_credits': [{'id': 'b5', 'gross_amount': '30000.00', 'currency': 'INR', 'ts': '2026-08-25T09:00:00', 'refs': {'utr': 'UTR-5'}}]}), encoding='utf-8')"
python -m reclaim big.json --store ./run --value-threshold 15000
```
```
human queue: 1 gate/s awaiting -> run/gates.jsonl  (answer with --queue --decide)
```

```bash
python -m reclaim --queue ./run
```
```
human queue: 1 awaiting / 1 total  (run/gates.jsonl)
  [value_threshold] gate:value:leak:short:s5 20000.00 INR
      Sign off chasing 20000.00 INR, above the 15000.00 INR threshold?
      - leak type short_payment
      - bank credit 30000.00 INR is below expected payout 50000.00 INR
  parked behind unanswered questions: 20000.00 INR
```

`parked behind unanswered questions` is the cost of the queue, stated rather
than left to be discovered. Now answer it — with a name and an explicit
timestamp, because the engine never reads the clock (G4) and a decision with no
name on it is not an audit trail:

```bash
python -m reclaim --queue ./run --decide gate:value:leak:short:s5 \
    --verdict approve --actor ops@example.com --at 2026-08-26T11:15:00
python -m reclaim --queue ./run          # a different process; the answer survived
```
```
approved: gate:value:leak:short:s5 by ops@example.com at 2026-08-26T11:15:00
  0 gate/s still awaiting a human

human queue: 0 awaiting / 1 total  (run/gates.jsonl)
  (approved by ops@example.com) gate:value:leak:short:s5
```

**A gate is decided once.** Two approvals of one high-value debit is the failure
mode that costs a merchant real money, so it is refused by the state machine
rather than by a UI that hides the button:

```bash
python -m reclaim --queue ./run --decide gate:value:leak:short:s5 \
    --verdict reject --actor someone.else@example.com --at 2026-08-26T12:00:00
```
→ `error: gate 'gate:value:leak:short:s5' is already approved (decided by
'ops@example.com'); a gate is decided once` · **exit 2**

Approval **authorises but does not act** — money moves only when `control.resume`
runs the bounded saga, under the same notice window, AFA ceiling and idempotency
keys. A human "yes" on an over-ceiling debit still halts.

## 10. Measured causal lift — the T+1 observation loop

```bash
python -m reclaim.experiment
```
```
  treated / control   : 135 / 65
  T+1 observation (outcomes read from the follow-up batch):
    treated recovered : 93/135  (rate 0.6889)
    control recovered : 27/65  (rate 0.4154)
    lift              : 27.3500 pp
    underpowered      : False
  the three numbers, in descending order of honesty:
    claimed  (engine said RECOVERED)      : 27000.00 INR
    observed (bank data at T+1, treated)  : 18600.00 INR
    causal   (net of the control cohort)  : 7384.20 INR
  claimed but not observed : 42   <- engine said RECOVERED, bank data did not agree
  notice compliance        : 1.0000
```

Three numbers, one loop. `claimed` is what the executor reported. `observed` is
what the next bank file actually showed. `causal` is what is left after
subtracting what the control cohort recovered without being touched — the only
figure a merchant is really buying.

**`claimed but not observed: 42`** is the engine auditing its own success metric:
42 of 135 treated units were reported `RECOVERED` and the follow-up batch
disagreed. Outcomes are read from bank data for *both* arms, never from the
executor's claim, because asymmetric evidence between arms biases flattering
every time ([ADR-0022](decisions/ADR-0022-observation-loop.md)).

## 11. Learn, then target — the two-cycle demo

The measurement earns its keep here. Cycle 1 chases everyone; its observed
outcomes fit an uplift model; cycle 2 chases only what the model calls
persuadable ([ADR-0024](decisions/ADR-0024-uplift-targeting.md)).

```bash
python -m reclaim.cycles
```
```
  cohort: 900 short-paid settlements, three authored behaviours
    sure thing       200.00  contacted 90% / untouched 85%  (+5pp)
    persuadable     1500.00  contacted 65% / untouched 25%  (+40pp)
    sleeping dog    8000.00  contacted 25% / untouched 55%  (-30pp)

  what cycle 1's observed outcomes taught the model:
       200.00 shortfall -> sure_thing   uplift 0.1166  => skip
      1500.00 shortfall -> persuadable  uplift 0.4228  => chase
      8000.00 shortfall -> sleeping_dog uplift -0.3926  => skip

                                     cycle 1             cycle 2
                            (chase everyone)          (targeted)
  units contacted                        612                 204
  contacts made                          612                 204
  skipped by targeting                     0                 408
  value skipped                        0 INR      1680600.00 INR
  lift (pp)                           4.7200             18.3800
  claimed recovered           1986600.00 INR       306000.00 INR
  observed recovered           629300.00 INR      1164400.00 INR
  CAUSAL recovered            -508425.82 INR       172093.30 INR
  causal per contact               -830.7611            843.5946
```

Read the bottom two rows. **Chasing everyone claimed ₹19.9 lakh recovered and
destroyed ₹5.08 lakh of value**, because the ₹8,000 cohort recovers *better* when
left alone. Targeting made one third of the contacts and turned that into
**+₹1.72 lakh causal** — from −₹830.76 to +₹843.59 per contact.

The model was told nothing about which shortfall means which behaviour; it read
all three out of one period of observed bank outcomes. A response model would
have ranked the sure things first, because 90% is the highest recovery rate on
the sheet.

Both cohorts keep the full behaviour mix in both cycles: "treated" means the
policy ran, which in cycle 2 includes units it chose not to contact. That keeps
the lift a statement about the deployed policy rather than about a subset it
selected — and is why `run_reclaim` assigns cohorts *before* targeting.

> **The batches and the three behaviour profiles are synthetic**, so the *size* of
> the improvement is a property of the fixture. The direction is not: given those
> behaviours, skipping them is arithmetically better, and the code finds which
> ones to skip unaided.

## 12. Reset

```bash
rm -rf run demo.key big.json  # PowerShell: Remove-Item -Recurse -Force run, demo.key, big.json
```

## Honest limits

What this demo does **not** prove, stated plainly so nobody has to infer it:

| Claim | Status |
|---|---|
| Detection, fee decomposition, fuzzy matching, ledger balance | **Real** — deterministic, 830 tests |
| Merkle audit, inclusion proofs, anchored replay, tamper detection | **Real** — demonstrated in step 7 |
| Event-sourced persistence, idempotent re-persist, byte-stable artifacts | **Real** — steps 4–6 |
| Recovery *outcomes* (₹200 recovered) | **Simulated** — `AlwaysSucceedsExecutor`, not a payment rail |
| RBI 24-hour pre-debit notice | **Real seam, no channel wired** — with a `NoticeExecutor` the notice is dispatched and a rejected notice halts the debit; the demo supplies none, so the scorecard reports `notice_compliance: 0` |
| LLM exception resolution | **Real code, not exercised here** — `llm_resolver` needs a key; the demo uses deterministic fakes |
| Blocking, Leak Ledger, consistency proofs, AFA ceiling | **Real** — ADRs 0017–0020 |
| Causal lift arithmetic + deterministic holdout | **Real** — `measurement`, ADR-0021 |
| A *measured* lift figure | **Real** — the T+1 observation loop reads outcomes from the follow-up batch for both arms and reports `claimed_not_observed` where the engine's claim disagrees (steps 10–11, ADR-0022). The *batches* are synthetic, so the printed lift is a property of the fixture, not evidence about real payments |
| HITL gates — pause, persist, resume, decided-once | **Real** — step 9, ADR-0023 |
| Uplift targeting (four-quadrant persuadability) | **Real** — fitted from observed outcomes, wired into `run_reclaim`, demonstrated in step 11 (ADR-0024) |
| Funded-moment predictor + scheduler seam | **Real and refusing** — the seam is advisory: a proposal at or before the notice deadline halts the recovery (ADR-0025). **But nothing sources funded moments** — no per-customer credit feed exists, so the predictor is fitted from caller-supplied history |
| `Context.prior_failures` | **Unsourced** — required rather than defaulted, so the gap is visible at every call site |
| Contextual bandit + IPS/DR offline evaluation | **Real** — ε-greedy with exact propensities (chosen so the evaluator is unbiased), IPS + doubly-robust, and a `should_deploy` gate that returns `None` rather than a number when the log cannot identify the policy (ADR-0027). Wired into the engine via `ActionPolicy`, with the reward taken from the T+1 bank data |
| Drift monitoring | **Real** — reward drift and action-mix drift reported separately, `INSUFFICIENT_DATA` below `min_n` (ADR-0029). **Not scheduled** — a function, not a job |
| Cross-run contact caps and consent state | **Real and durable** — consent defaults to *deny*, quiet hours, cooling-off, caps that span runs, and repositories so a cap survives a restart (ADR-0028, ADR-0029). **No shipped command writes them**, because nothing shipped contacts anyone |
| Medallion ingestion (Bronze → Silver → Gold) | **Real** — content-addressed Bronze, an arithmetic "nothing dropped" invariant, reference normalisation, confidence-gated UTR extraction, duplicate-UTR quarantine (ADR-0030). **Bronze is in memory**, so it replays within a process, not across a restart |
| Kafka / CDC / streaming ingestion | **Not implemented** — the ingest layer is batch with medallion *semantics*. **G10 (scale) remains outstanding** |
| Account Aggregator, PDF statements, LLM extraction | **Not implemented** — the `NarrationExtractor` seam exists with a deterministic regex behind it; no model is called |
| Per-customer funded-moment history and `prior_failures` | **Unsourced** — `customer_ref` now makes both assemblable from the Leak Ledger; nothing assembles them yet |

Both external integrations (`ChatClient` → Anthropic, `PaymentGateway` → Razorpay test mode) are
real drop-ins behind protocol seams with deterministic fakes, which is why the whole system is
provable offline — but "provable offline" is not the same as "proven in production".

---

**Environment used for the outputs above:** Python 3.13.9 on Windows 11; also runs on 3.11+, any OS.
Deeper reference: [`../README.md`](../README.md) (CLI flags), [`BUILD-LOG.md`](BUILD-LOG.md)
(what was built and how it was tested), [`decisions/`](decisions/) (21 ADRs).
