# Track 01 — AI Growth & Agentic Commerce — India Value-Chain Grid

> India primary / global secondary · *synthesis, not prescription* · every non-trivial claim carries a **confidence** level and a real source · built on `00-shared-value-chain.md` · **light** research pass (targeted searches; gaps flagged rather than padded).
>
> **Track bar (from brief):** every money action explainable, bounded and gated; show the audit trail and one failure handled gracefully.

## 0. Where this track sits on the value chain
**S5 Customer Interface** (conversational/agentic checkout) + **S6 End Users** (the emerging *AI buyer agent* as a new user class) + **S4 Distribution** (making a merchant discoverable/transactable to agents). It is the frontier stage of the spine — money moving through an **agent** instead of a human tapping a screen.

## 1. Market Size
**No credible standalone "India agentic-commerce market size" exists yet — the category is ~months old. I cannot confirm a figure; anyone quoting one is extrapolating.** *(Confidence: High that no reliable figure exists.)*

Defensible framing instead — size the *host* markets it rides on:
- India digital-commerce payments throughput is the denominator: UPI ≈ **₹314 lakh crore** value in FY25-26 (see `00-shared-value-chain.md`). *(High.)*
- Proxy value lever: conversational commerce reportedly drives **3–5× conversion** vs traditional funnels and **+8–25% AOV** via in-context upsell/cross-sell ([BigCommerce](https://www.bigcommerce.com/articles/ecommerce/conversational-commerce/), [Envive](https://www.envive.ai/post/how-ai-improves-cross-selling-and-upselling-in-ecommerce)). *(Confidence: Low-Medium — vendor-reported, not independently audited; treat as directional.)*

**Bottom-up "revenue uplift" framing (the honest way to size this track):**
```
Merchant monthly GMV (assume mid D2C)                 ₹50,00,000   [ASSUMPTION]
× AOV uplift from agentic upsell/cross-sell           × 10%        [low end of 8–25%, ASSUMPTION]
= incremental GMV / merchant / month                  ₹5,00,000
```
So the value proposition is a **% of incremental GMV**, not a fee on a fixed rail. The swing factor is the *real, audited* uplift — currently unproven at scale in India. **Confidence: method Medium, absolute Low.**

## 2. Key Players
### Large corporates / incumbents
- **NPCI** — building the **Unified Agent Protocol (UAP)** to allow AI agents to make UPI payments as national infrastructure ([Business Standard](https://www.business-standard.com/finance/news/india-may-allow-agentic-ai-led-upi-transactions-under-new-npci-protocol-126070801343_1.html), [Medianama](https://www.medianama.com/2026/07/223-npci-agentic-payments-upi/)). *(High.)*
- **Razorpay** — with NPCI, launched **agentic UPI payments on Claude (Feb 2026)**, initial partners Zomato, Swiggy, Zepto; consent-based one-time auth with per-merchant spend limits ([Stellagent](https://stellagent.ai/insights/india-npci-unified-agent-protocol-upi)). *(Medium-High — reported; primary Razorpay PR not fetched.)*
- **Pine Labs** — has its own **P3P (Pine Labs Payments Protocol)** in the agentic-protocol race ([Applied Technology Index](https://appliedtechnologyindex.com/research/2026-comparative-analysis-agentic-commerce-payment-protocols/)). *(Medium.)*
- Global overlays: **Visa TAP**, **Mastercard** network overlays, **Google AP2**, **OpenAI ACP**, **Coinbase x402** ([Medium/Adnan Masood](https://medium.com/@adnanmasood/agentic-payments-101-2-2-payment-standards-and-protocols-acp-ucp-ap2-and-x402-26486e6d511f), [Crossmint](https://www.crossmint.com/learn/agentic-payments-protocols-compared)). *(Medium-High.)*

### Startups
- Conversational/upsell tooling (mostly global, India adoption via WhatsApp/JioMart): Session AI, Selleasy, Quickchat, Envive. *(Low-Medium — vendor pages, not India-specific.)*
- **India-specific agentic-commerce startups: I could not verify a clear list in this light pass — flagged for deeper research.**

### Investors (VC / PE / angels)
- **Not verified in this pass.** Would expect the usual India-fintech backers (Peak XV, Accel, Lightspeed, Elevation) but I have **no sourced funding round** to cite here. *(Confidence: cannot confirm.)*

## 3. Technology Trends
- **Protocol layering** is the defining trend: intent/authorization layer (AP2, Verifiable Intent) vs checkout layer (ACP/UCP) vs machine-to-machine settlement (x402 stablecoin over HTTP 402) vs merchant-edge auth (TAP, Web Bot Auth). Drivers: Google, OpenAI, Visa/Mastercard, Coinbase, NPCI (India). ([honeyb.ai](https://www.honeyb.ai/blog/agentic-commerce-protocols)) *(Medium-High.)*
- **Consent + bounded-authority patterns** (per-merchant spend caps, one-time pre-auth) are emerging as the safety primitive — directly matches this track's "bounded and gated" bar. *(Medium.)*
- **Security is an open problem**: a systematic security analysis of x402 ("Free-Riding the Agentic Web", arXiv) shows the space is immature. ([arXiv](https://arxiv.org/pdf/2605.30998)) *(Medium — signals risk, and a build opportunity.)*

## 4. Demand-Side Trends
- Indian conversational-commerce demand is real via **WhatsApp/JioMart** order flows ([BigCommerce](https://www.bigcommerce.com/articles/ecommerce/conversational-commerce/)). *(Low-Medium.)*
- Consumer willingness to let an **agent transact** is nascent and unproven at scale in India — the Zomato/Swiggy/Zepto pilot is the leading indicator, not proof of mass demand. *(Confidence: Low — pilot-stage.)*

## 5. Supply-Side Trends
- Rails are being *opened to agents* top-down (NPCI UAP, Razorpay APIs) — supply is being manufactured by infrastructure players, not pulled by merchants yet. *(Medium.)*
- Merchant catalogs are not yet "agent-readable" by default — a supply gap the brief explicitly names ("agent-readable catalog"). *(Medium.)*

## 6. Policies & Regulations
- **NPCI UAP** is the governing framework-in-progress for agent-led UPI; how much authority an agent may hold is being defined now. ([Medianama](https://www.medianama.com/2026/07/223-npci-agentic-payments-upi/)) *(Medium-High.)*
- Inherits **RBI PA/PG Master Direction 2025** (money movement) and **DPDP** (agent handling personal + payment data). *(High — see spine.)*
- **Open regulatory question (cannot confirm):** liability when an agent makes a wrong purchase; the consent/audit-trail requirement is precisely why this track's "audit trail + one failure handled" bar matters.

## 7. Summary & Storyline
India is a **live pilot ground, not a follower**, for agent-to-agent commerce: NPCI's UAP + the Razorpay/Claude pilot put it ahead of most markets. The value chain is being *rebuilt at S5/S6* around a new user — the buyer agent. Margin logic follows the spine: rails stay near-free, so value accrues to whoever makes a merchant **safely, auditably transactable by agents** and whoever converts agentic interaction into **incremental GMV**. The binding constraints are **trust, bounded authority, and auditability** — verification, not generation. *(Confidence: Medium — thesis is well-supported directionally; adoption data is thin.)*

## 8. Market Gaps & Opportunities
**Gap A — Agent-readable catalog + safe checkout for the long tail of merchants.**
- *Hypothesis (demography):* mid-market Indian D2C brands (₹1–50 cr GMV) already on Razorpay who cannot afford to build agent integrations themselves. *(Medium.)*
- *Solution outline:* a service that exposes a merchant's catalog + a **bounded, gated, audit-logged** checkout to buyer agents on Razorpay test-mode APIs. *Assumptions to validate:* that buyer-agent traffic will materialise; that merchants will pay before proven GMV. 
- *Moat:* audit-trail/compliance depth + being early on UAP; weak moat on the catalog format itself (likely to standardise).
- *Confidence: Medium.* Directly satisfies the bar (explainable/bounded/gated + audit trail).

**Gap B — The "one failure handled gracefully" layer: agentic-transaction guardrails & dispute handling.**
- *Hypothesis:* the first wave of agentic commerce will produce wrong/duplicate/over-limit purchases; merchants and PSPs need a control plane. *(Medium.)*
- *Solution:* real-time policy engine (spend caps, merchant allow-lists, reversible holds) + graceful failure/rollback + audit log. *Assumptions:* volume of agentic errors high enough to matter soon.
- *Moat:* trust + integration with rails; compounds with data. *Confidence: Medium.* Note: the x402 security paper suggests this need is real.

**Gap C — Incremental-GMV upsell/cross-sell agent with *proven, audited* uplift (India).**
- *Hypothesis:* Indian merchants will pay a % of *incremental* GMV, not a SaaS fee, if uplift is honestly measured. *(Low-Medium.)*
- *Solution:* conversational upsell agent with a **held-out control group** to prove causal uplift. *Assumptions:* the 8–25% global uplift transfers to India (unproven).
- *Moat:* the measurement/attribution rigor itself (most vendors quote uplift without controls). *Confidence: Low-Medium.*

## 9. Validation & Discovery
| Dimension needing validation | Who to talk to | What to confirm |
|---|---|---|
| Real, audited AOV/conversion uplift in India | 10–15 D2C merchants; a CRO/experimentation lead | Does agentic upsell beat a control group? by how much? |
| Buyer-agent traffic reality | Razorpay agentic-commerce PM; NPCI UAP team | Actual/expected agent transaction volume; UAP timeline & authority limits |
| India agentic-commerce startup & funding landscape | India fintech VC (Peak XV/Accel/Elevation) | Who's building here; what's funded (this pass could NOT verify) |
| Regulatory liability for agent errors | Payments compliance counsel | Who is liable; consent/audit requirements under UAP |
| Consumer trust in agent-led spend | Consumer research / pilot data (Zomato/Swiggy/Zepto) | Opt-in & repeat-use rates |

## Sources
- [Business Standard — India may allow agentic AI-led UPI under new NPCI protocol](https://www.business-standard.com/finance/news/india-may-allow-agentic-ai-led-upi-transactions-under-new-npci-protocol-126070801343_1.html)
- [Medianama — How NPCI should approach agentic payments](https://www.medianama.com/2026/07/223-npci-agentic-payments-upi/)
- [Stellagent — NPCI Unified Agent Protocol / UPI agentic payments](https://stellagent.ai/insights/india-npci-unified-agent-protocol-upi)
- [Medium (Adnan Masood) — Agentic Payments 101: ACP, UCP, AP2, x402](https://medium.com/@adnanmasood/agentic-payments-101-2-2-payment-standards-and-protocols-acp-ucp-ap2-and-x402-26486e6d511f)
- [Crossmint — Agentic payments protocols compared (MPP, ACP, AP2, x402)](https://www.crossmint.com/learn/agentic-payments-protocols-compared)
- [honeyb.ai — Agentic Commerce Protocols Explained (ACP, AP2)](https://www.honeyb.ai/blog/agentic-commerce-protocols)
- [Applied Technology Index — 2026 agentic commerce payment protocols (incl. Pine Labs P3P)](https://appliedtechnologyindex.com/research/2026-comparative-analysis-agentic-commerce-payment-protocols/)
- [arXiv — Free-Riding the Agentic Web: security analysis of x402](https://arxiv.org/pdf/2605.30998)
- [BigCommerce — Conversational Commerce in 2026](https://www.bigcommerce.com/articles/ecommerce/conversational-commerce/)
- [Envive — How AI improves cross-selling & upselling](https://www.envive.ai/post/how-ai-improves-cross-selling-and-upselling-in-ecommerce)
