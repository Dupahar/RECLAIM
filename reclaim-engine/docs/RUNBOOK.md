# RECLAIM — Demo Runbook

Reproduce the full loop in ~2 minutes. **No install, no network, no API keys, no dependencies**
beyond Python 3.11+ (`pytest` only for step 1). Every output below is verbatim from a real run,
with paths shown POSIX-style (Windows prints `run\root.txt`) and key-dependent signatures elided.

> **Scope of this demo.** Detection, booking, audit and replay-verification are real. **Recovery
> outcomes are a deterministic simulation** — the demo uses a stand-in executor behind the tested
> `PaymentGateway` seam. RECLAIM does not send pre-debit notices, move real money, or measure causal
> uplift against a control group. The RBI 24-hour window is *modelled* (notice time recorded,
> attempts scheduled after it), not *executed*. See [Honest limits](#honest-limits).

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

## 1. Tests — 420 passing, 100% line + branch

```bash
python -m pytest --cov=reclaim --cov-branch
```
→ `TOTAL 2025 statements, 554 branches, 0 missed, 100%` · `420 passed`

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

## 3. Reconcile a real batch file (detection-only — safe, no side effects)

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

## 7b. The scorecard and the Leak Ledger

```bash
python -c "from reclaim.demo import run_demo; from reclaim.scorecard import build_scorecard; import json; print(json.dumps(build_scorecard(run_demo()).summary(), indent=2))"
```
Note two fields that stay honest by default: `causal_recovered` is `null` with the note *"no holdout
was run, so recovery cannot be credited causally"*, and `notice_compliance` is `0.0000` because the
demo supplies no `NoticeExecutor`. Both become real numbers only when the machinery behind them is
actually used — see [ADR-0021](decisions/ADR-0021-causal-measurement.md).

The Leak Ledger persisted in step 5 is the human queue:

```bash
python -c "from reclaim.demo import run_demo; from reclaim.pipeline import build_leak_ledger; ll = build_leak_ledger(run_demo()); print('open:', [l.id for l in ll.open_queue()]); print('states:', {l.id: l.recovery_state.value for l in ll.leaks()})"
```
Four leaks were detected; one was recovered, two were `superseded` by a later fuzzy match (never
real leaks), and exactly one is open — the same single leak the report calls residual.

## 8. Reset

```bash
rm -rf run demo.key          # PowerShell: Remove-Item -Recurse -Force run, demo.key
```

## Honest limits

What this demo does **not** prove, stated plainly so nobody has to infer it:

| Claim | Status |
|---|---|
| Detection, fee decomposition, fuzzy matching, ledger balance | **Real** — deterministic, 420 tests |
| Merkle audit, inclusion proofs, anchored replay, tamper detection | **Real** — demonstrated in step 7 |
| Event-sourced persistence, idempotent re-persist, byte-stable artifacts | **Real** — steps 4–6 |
| Recovery *outcomes* (₹200 recovered) | **Simulated** — `AlwaysSucceedsExecutor`, not a payment rail |
| RBI 24-hour pre-debit notice | **Real seam, no channel wired** — with a `NoticeExecutor` the notice is dispatched and a rejected notice halts the debit; the demo supplies none, so the scorecard reports `notice_compliance: 0` |
| LLM exception resolution | **Real code, not exercised here** — `llm_resolver` needs a key; the demo uses deterministic fakes |
| Blocking, Leak Ledger, consistency proofs, AFA ceiling | **Real** — ADRs 0017–0020 |
| Causal lift arithmetic + deterministic holdout | **Real** — `measurement`, ADR-0021 |
| A *measured* lift figure | **Not yet produced** — cohorts are assigned and the maths is tested, but crediting real lift needs the T+1 re-reconciliation loop to observe whether held-out leaks self-resolved. Deliberately not faked |
| Uplift model / funded-moment predictor / bandit | **Not implemented** — Sprint 3 |

Both external integrations (`ChatClient` → Anthropic, `PaymentGateway` → Razorpay test mode) are
real drop-ins behind protocol seams with deterministic fakes, which is why the whole system is
provable offline — but "provable offline" is not the same as "proven in production".

---

**Environment used for the outputs above:** Python 3.13.9 on Windows 11; also runs on 3.11+, any OS.
Deeper reference: [`../README.md`](../README.md) (CLI flags), [`BUILD-LOG.md`](BUILD-LOG.md)
(what was built and how it was tested), [`decisions/`](decisions/) (21 ADRs).
