<div align="center">

# RECLAIM

### Reconciliation-Enabled Closed-Loop AI for Integrity & Money-recovery

**Find every rupee that leaks. Win back what's winnable. Prove the books closed.**

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-322%20passing-2ea44f)
![Coverage](https://img.shields.io/badge/coverage-100%25%20line%20%2B%20branch-2ea44f)
![License](https://img.shields.io/badge/license-MIT-0b3b8f)
![Status](https://img.shields.io/badge/build-passing-2ea44f)

`fintech` · `reconciliation` · `revenue-recovery` · `payments` · `double-entry-ledger` · `audit` · `python`

</div>

---

RECLAIM is a closed-loop revenue-integrity engine for online businesses. It reconciles a merchant's money across sources to **detect** every leak, **recovers** the recoverable ones through a bounded, compliant, idempotent workflow, and **re-reconciles to prove the books closed** — leaving a tamper-evident audit trail of every decision, including the actions it deliberately does *not* take.

The design principle is **verification over generation**: a deterministic core owns all money math, and AI-assisted exception resolution is a *gated proposal* that fails safe to a human — it can never cause a wrong match or move money on a guess.

## The problem

Every business that moves money online loses a slice of revenue to **silent leakage** — money that fails to arrive, or arrives short, or is never chased. In India the leak points are large and well-documented:

| Leak point | Scale | Source |
|------------|-------|--------|
| **UPI Autopay** (recurring debits) | Fails **8–15%** of the time vs **2–3%** for card mandates — and much of it is *temporary* (a momentary low balance / bank downtime), i.e. **recoverable**. Late-2025 reporting puts Autopay *success* as low as 30–50%. | [productgrowth.in](https://productgrowth.in/insights/fintech/upi-autopay-guide/), [Mint](https://www.htsyndication.com/mint/article/upi-autopay-s-recurring-woes-are-forcing-an-industry-rethink/93925664) |
| **Checkout / payment success** | Blended merchant success sits at **92–96%**; below 90% is "a serious business problem." Every failed payment is revenue at risk. | [productgrowth.in](https://productgrowth.in/insights/fintech/upi-payment-success-rates/) |
| **Returns (D2C RTO)** | **20–40%** of orders return to origin (fashion 40%+); COD RTO **~26%** vs **<2%** prepaid, at a fully-loaded **₹450–900 per undelivered COD parcel**. | [ClickPost](https://www.clickpost.ai/blog/rto-reduction-tools), [HillTeck](https://www.hillteck.com/blog/reduce-rto-ecommerce-india.html) |
| **Settlement complexity** | Every gateway payout arrives net of stacked deductions (MDR + GST-on-MDR + TCS) and is still reconciled **by hand in spreadsheets**. | [AI Accountant](https://www.aiaccountant.com/blog/payment-reconciliation-platform-india-2025) |
| **Fraud (India, RBI)** | Bank fraud reached **₹48,021 cr in FY26** (up from ₹36,014 cr in FY25). | [Business Standard / RBI](https://www.business-standard.com/finance/news/bank-fraud-amount-triples-in-fy25-despite-drop-in-number-of-cases-rbi-125052900696_1.html) |

**This is a winnable problem — proven at scale.** Stripe's recovery tooling reclaimed **$6 billion** in falsely-declined payments in 2024 alone, and Deliveroo won back **£100 million+** using smart retries. The money is recoverable; most businesses just never close the loop. ([Stripe: Adaptive Acceptance](https://stripe.com/blog/ai-enhancements-to-adaptive-acceptance), [Stripe: Smart Retries](https://stripe.com/blog/how-we-built-it-smart-retries))

### Why existing tools don't solve it

- **Reconciliation tools** find *what* money didn't arrive — then hand a dead exception list to a human. They detect; they don't recover.
- **Dunning / recovery tools** retry a *known* failed debit — but they're blind to leaks that don't look like failed debits (short settlements, missing payouts, abandoned checkouts), and they can't prove they recovered everything.

Two half-loops, no one closes it. And because **UPI runs at zero MDR**, margin has moved off the rails and onto exactly this kind of value-added service — reconciliation, risk, and recovery.

### The need

A single system that **detects every leak, recovers what's recoverable within compliance limits, and proves the books closed** — with a tamper-evident audit trail. That is RECLAIM.

## Highlights

- **Closed loop** — detect → recover → book → audit → persist → verify, in one system.
- **Exact money** — `Decimal` end to end, never floating point, enforced from the core to the I/O boundary.
- **Two-brain reconciliation** — an exact deterministic gate, a probabilistic (fuzzy) matcher, and an AI-assisted, adversarially-verified resolver for the ambiguous middle.
- **Bounded, compliant recovery** — RBI 24-hour pre-debit notice, capped attempts, stopping rules, and idempotent charges (no double-billing).
- **Provable integrity** — an immutable double-entry ledger (`debits == credits`), a Merkle transparency log with inclusion proofs, and optional HMAC signing.
- **Durable & replayable** — event-sourced persistence; a stored run can be re-verified and tamper-detected at any time.
- **Fully tested** — 18 modules, **322 tests, 100% line + branch coverage**.

## The loop

```mermaid
flowchart LR
    A["Ingest<br/>JSON / CSV"] --> B{"Reconcile<br/>exact gate"}
    B -->|"ties out"| L["Double-entry<br/>ledger"]
    B -->|"gap"| F["Probabilistic<br/>matcher"]
    F -->|"auto"| L
    F -->|"review"| R{"AI resolver<br/>gated + verified"}
    R -->|"confirmed"| L
    R -->|"uncertain"| H["Escalate<br/>to human"]
    B -->|"recoverable leak"| RC["Recovery engine<br/>bounded · compliant"]
    RC -->|"recovered"| L
    RC -->|"exhausted / dead"| H
    L --> AU["Merkle audit log<br/>+ signature"]
    AU --> P["Persist<br/>event-sourced"]
    P --> V["Replay-verify<br/>tamper detection"]
    L --> RP["Report<br/>match rate · recovered · residual"]
```

## Quick start

```bash
cd reclaim-engine

python -m pytest --cov=reclaim --cov-branch      # 322 tests, 100% line + branch
python -m reclaim.demo                            # run the full loop (demo data)
python -m reclaim examples/sample_batch.json      # reconcile a JSON batch
python -m reclaim --csv examples/sample_batch.csv # …or CSV
```

Persist a run and verify it later (tamper-evident):

```bash
python -m reclaim examples/sample_batch.json --store ./run --key-file key.bin
python -m reclaim --replay ./run --expect-root <ROOT> --key-file key.bin
```

Open `reclaim-engine/examples/demo_dashboard.html` for the visual run report.

## Architecture

| Layer | Module | Responsibility |
|-------|--------|----------------|
| Money | `money` | Exact, currency-safe `Decimal` value type |
| Domain | `domain` | Canonical Transaction / Fees / LedgerEntry / LeakRecord |
| Ledger | `ledger` | Immutable, idempotent double-entry ledger (`debits == credits`) |
| Detect | `reconciliation` | Exact settlement↔bank matching + typed leak detection |
| Detect | `probabilistic` | Fellegi-Sunter-style fuzzy matcher (auto / review / residual) |
| Detect | `resolver` · `llm_resolver` | Gated resolver (self-consistency + confidence + adversarial verifier); LLM backend |
| Recover | `recovery` · `payments` | Diagnosis + bounded compliant workflow; payment-gateway executor |
| Integrity | `audit` · `signing` | Merkle transparency log + inclusion proofs; HMAC signing |
| Durability | `persistence` · `verification` | Event-sourced storage; replay + tamper verification |
| Orchestration | `pipeline` · `cli` · `dashboard` | End-to-end run, CLI, HTML report |

Every significant design decision is recorded as an Architecture Decision Record in [`reclaim-engine/docs/decisions/`](reclaim-engine/docs/decisions/).

## Repository layout

```
reclaim-engine/          the engine (src layout, pytest)
  src/reclaim/           18 modules
  tests/                 322 tests, 100% line + branch
  docs/decisions/        Architecture Decision Records
  examples/              sample batches + demo dashboard
RECLAIM-Product-Document.md        product & company vision
RECLAIM-System-Architecture.md     system design
fintech-grid/                      India-first market research (value-chain grids)
```

## Engineering guarantees

- Money is exact (`Decimal`), never float.
- Money actions are idempotent — a retried recovery never double-charges.
- Any resolver or gateway failure degrades to "escalate to a human," never a wrong match.
- Runs are deterministic and replayable — same inputs produce the same audit root.
- The core runs on the Python standard library; external integrations are optional, behind clean interfaces.

## License

Released under the [MIT License](LICENSE).

## Author

**Adil Mahajan** — University of Jammu.
