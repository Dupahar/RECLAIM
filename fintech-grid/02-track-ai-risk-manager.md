# Track 02 — AI Risk Manager — India Value-Chain Grid

> India primary / global secondary · *synthesis, not prescription* · confidence + real source on every non-trivial claim · built on `00-shared-value-chain.md` · **light** research pass.
>
> **Track bar (from brief):** honest metrics including false-positive cost; a working detector/verifier/auto-responder for ONE class of loss with measured precision & recall on a held-out set. **Strictly defense-only — anything offense-capable is disqualified.**

## 0. Where this track sits on the value chain
**S7 Post-Transaction Lifecycle** — specifically the risk/fraud/returns/chargebacks sub-loop. It protects money *after* it moves (or *at* authorization), so its value is measured in **loss avoided**, netted against **false-positive cost** (good customers wrongly blocked).

## 1. Market Size
Sized as **loss pools** (the money at stake), since that is what the product recovers.

| Loss pool | Figure | Source | Confidence |
|---|---|---|---|
| Consumer digital-payment **scam** losses, India | **≈ US$2.5 bn in 2025** (claimed +4,300% since 2021) | [Emirates 24/7](https://www.emirates247.com/world/25-billion-lost-to-digital-payment-scams-in-2025-spurs-rbi-action-in-india/1228) | Low-Medium (secondary; large round number) |
| RBI-reported **bank fraud** (all categories) | **FY25: ₹36,014 cr across 23,953 cases** (value ~+194% YoY even as cases fell from 36,060); **FY26: rose to ₹48,021 cr** (cases roughly halved) | [Business Standard / RBI (FY25)](https://www.business-standard.com/finance/news/bank-fraud-amount-triples-in-fy25-despite-drop-in-number-of-cases-rbi-125052900696_1.html), [FY26 ₹48,021 cr](https://www.thehansindia.com/business/banks-report-rs-48000-crore-frauds-1080926) | Medium-High (RBI-sourced) |
| Card & internet-banking fraud (RBI category) | **~₹520 cr across 13,516 cases in FY25** — high-frequency but low-value; **~92% of total fraud *value* (₹33,148 cr) was advances/loan fraud** | same RBI report | Medium-High |

> **Conflict flagged:** the "$2.5bn scam" figure and RBI's card-fraud value are **not contradictory — they measure different things** (consumer *scam/social-engineering* losses vs RBI's *reported card-fraud* category). Do not add them. **The fraud *value* is dominated by advances/loan fraud (~92%); card & internet-banking fraud is high-frequency but low-value (~₹520 cr).** *(Confidence: Medium.)* *(Correction: an earlier draft cited a "₹29 cr / 293-case FY26 card-fraud collapse" — that figure could not be verified and has been removed.)*

**Returns/RTO loss pool (the merchant-side, often bigger for D2C):**
```
D2C RTO rate                        20–35% (40%+ fashion)     [ClickPost/HillTeck, Med]
COD RTO vs prepaid                  ~26% COD vs <2% prepaid   [ClickPost, Med]
Fully-loaded cost / undelivered COD ₹450–₹900                 [HillTeck, Low-Med]
```
Bottom-up per merchant: 10,000 COD orders/mo × 26% RTO × ₹600 loaded cost = **₹15.6 lakh/mo lost to RTO** for one mid D2C brand. *(Method Medium; inputs merchant-specific — validate.)*

## 2. Key Players
### Large corporates / incumbents
- **RBI** — deploying its own ML fraud model; Governor: "AI and AI alone" can limit AI-driven fraud; **Financial Fraud Risk Indicator (FRI)** credited with preventing ₹660 cr losses in 6 months ([NewsOnAir](https://www.newsonair.gov.in/government-prevents-%e2%82%b9660-crore-cyber-fraud-losses-in-6-months-using-fraud-risk-indicator)). *(Medium-High.)*
- **Razorpay** — **Magic Checkout** with AI fraud detection (formerly **Thirdwatch**, acquired) for COD/RTO risk ([ClickPost](https://www.clickpost.ai/blog/rto-reduction-tools)). *(Medium.)*
- Banks' internal fraud/risk ops (HDFC, ICICI, SBI). *(Medium — general knowledge.)*

### Startups
- **GoKwik** — checkout COD risk scoring / RTO reduction. *(Medium.)*
- **Bureau** — identity + RTO/return-fraud risk. *(Medium.)*
- **ClickPost, Shiprocket, Metaport** — NDR/courier intelligence + RTO analytics. *(Medium.)*
- Bank-fraud AI vendors e.g. **Innefu**. *(Low-Medium — vendor page.)*

### Investors (VC / PE / angels)
- **Not verified with a specific sourced round in this light pass.** GoKwik/Bureau are venture-backed but I will not cite amounts I did not verify. *(Cannot confirm specifics.)*

## 3. Technology Trends
- **Regulator-led ML** (RBI FRI, RBI's own model) — the state is now a fraud-detection player, not just a rule-setter. ([The Logical Indian](https://thelogicalindian.com/rbi-ai-fight-banking-fraud-india-121775/)) *(Medium.)*
- **Behavioural / device / velocity risk scoring** on every order (phone-number behaviour, address delivery history, order frequency). Drivers: GoKwik, Bureau, Razorpay/Thirdwatch. ([Bureau](https://bureau.id/resources/blog/solving-the-rto-challenge)) *(Medium.)*
- **Adversarial escalation:** synthetic-identity fraud, account takeover, AI-crafted phishing ([Jisa Softech](https://jisasoftech.com/how-indias-fintech-fraud-patterns-are-evolving-in-2025/)). *(Medium.)* → defense must be model-based because rules can't keep up.

## 4. Demand-Side Trends
- D2C brands with heavy social-ad traffic are acutely exposed to **fake-COD/bot orders and serial refusers** → strong pull for order-risk scoring. ([Metaport](https://metaport.in/cod-fraud-detection-prevention-ecommerce/)) *(Medium.)*
- Banks/consumers demanding fraud protection post-scam-surge; "second bank account for daily use" behaviour signals distrust ([Business Standard](https://www.business-standard.com/opinion/columns/indians-find-shield-against-fraud-second-bank-account-for-daily-use-125120700734_1.html)). *(Low-Medium.)*

## 5. Supply-Side Trends
- Consolidation of risk into the **checkout layer** (Razorpay Magic, GoKwik) — risk scoring bundled with checkout rather than sold standalone. *(Medium.)*
- Data-network effects: incumbents score against "millions of data points," a cold-start barrier for new entrants. *(Medium.)*

## 6. Policies & Regulations
- **RBI FRI** and RBI ML deployment set a de-facto baseline; alignment with RBI signals is becoming table stakes. *(Medium.)*
- Inherits **DPDP** (fraud models process personal data), **RBI PA/PG** norms, and card-network **chargeback/dispute rules** (Visa/Mastercard/RuPay) — the rulebook a chargeback-evidence responder must automate. *(Medium — specific rules not fetched this pass.)*
- **Track constraint = a compliance line:** defense-only. Anything that could profile/target for offense is disqualified *and* likely violates DPDP/RBI norms anyway. *(High — brief + regulatory logic.)*

## 7. Summary & Storyline
The fraud value pool is **large, shifting, and adversarially escalating** — moving from card fraud (now small per RBI) toward **loan fraud and social-engineering scams**, while **RTO/returns** quietly eat D2C margin at 20–40%. The regulator has itself become an AI fraud-fighter (FRI). The defensible wedge is not "detect fraud" in the abstract but **one class of loss, with honest precision/recall and an explicit false-positive cost** — because the real product cost is good customers wrongly blocked. Verification (is this flag *correct*?) is the bottleneck — the 2026 thesis. *(Confidence: Medium-High on the storyline.)*

## 8. Market Gaps & Opportunities
**Gap A — RTO/return-risk scorer with an honest false-positive ledger (D2C).**
- *Hypothesis (demography):* mid D2C brands (fashion/beauty, high COD, heavy social ads) losing ₹10L+/mo to RTO. *(Medium.)*
- *Solution:* order-risk model that outputs a score + the **cost of a wrong block** (lost good order), evaluated on a held-out set — exactly the track's bar. *Assumptions to validate:* merchants will accept some blocked-good-orders; label quality for "fraud vs genuine return."
- *Moat:* labelled outcome data + courier/NDR feedback loop; weak vs incumbents' data scale → **niche by category** (e.g. fashion) to win. *Confidence: Medium.*

**Gap B — Chargeback evidence responder (auto-assemble dispute packets).**
- *Hypothesis:* merchants lose winnable chargebacks because evidence assembly is manual and deadline-bound. *(Medium.)*
- *Solution:* agent that gathers order/delivery/comms evidence and drafts a network-compliant representment, with a **verifier** step (does the packet meet Visa/MC rules?). *Assumptions:* access to dispute data; win-rate uplift measurable.
- *Moat:* encoded dispute rulebooks + win-rate track record. *Confidence: Medium.* Strong fit to "verifier/auto-responder" + measurable metrics.

**Gap C — Fraud-spike / abuse-ring detector (defense-only).**
- *Hypothesis:* sudden coordinated abuse (bot COD, promo abuse) hits merchants faster than rules adapt. *(Medium.)*
- *Solution:* anomaly/graph detector flagging rings, with precision/recall on a held-out window and a human-gated action. *Assumptions:* enough labelled ring data. *Moat:* graph features + speed. *Confidence: Low-Medium.*

## 9. Validation & Discovery
| Dimension needing validation | Who to talk to | What to confirm |
|---|---|---|
| True false-positive cost tolerance | D2C founders/ops; risk leads at GoKwik-type firms | How many blocked-good-orders is acceptable per fraud caught? |
| Label quality (fraud vs genuine return) | Courier/NDR teams; merchant CX | Can outcomes be labelled reliably for training/eval? |
| Chargeback win-rate baseline | Merchant finance; PA dispute teams | Current win rate; what evidence wins under network rules |
| Which loss pool is biggest per segment | 10–15 merchants across categories | Rank fraud vs RTO vs chargebacks by ₹ |
| Startup funding/positioning (unverified here) | India fintech VCs | Who's funded, at what stage |

## Sources
- [Business Standard / RBI — bank fraud ₹48,021 cr FY26](https://www.business-standard.com/finance/news/bank-fraud-amount-triples-in-fy25-despite-drop-in-number-of-cases-rbi-125052900696_1.html)
- [Emirates 24/7 — $2.5bn digital payment scam losses 2025](https://www.emirates247.com/world/25-billion-lost-to-digital-payment-scams-in-2025-spurs-rbi-action-in-india/1228)
- [PwC India — Combating payments fraud in India's digital landscape](https://www.pwc.in/ghost-templates/combating-payments-fraud-in-Indias-digital-payments-landscape.html)
- [The Logical Indian — RBI on AI vs banking fraud](https://thelogicalindian.com/rbi-ai-fight-banking-fraud-india-121775/)
- [NewsOnAir — Govt prevents ₹660 cr fraud via FRI](https://www.newsonair.gov.in/government-prevents-%e2%82%b9660-crore-cyber-fraud-losses-in-6-months-using-fraud-risk-indicator)
- [Jisa Softech — evolving India fintech fraud patterns 2025](https://jisasoftech.com/how-indias-fintech-fraud-patterns-are-evolving-in-2025/)
- [ClickPost — RTO reduction tools for D2C India](https://www.clickpost.ai/blog/rto-reduction-tools)
- [Metaport — COD fraud detection & RTO](https://metaport.in/cod-fraud-detection-prevention-ecommerce/)
- [Bureau — solving the RTO challenge](https://bureau.id/resources/blog/solving-the-rto-challenge)
- [HillTeck — reduce RTO for Indian D2C (economics)](https://www.hillteck.com/blog/reduce-rto-ecommerce-india.html)
