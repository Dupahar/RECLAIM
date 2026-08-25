# ADR-0013 — Live payment executor behind a testable gateway seam

**Status:** Accepted · **Date:** 2026-08-26

## Context
Recovery ultimately has to move money (retry a debit). This is the last and
most sensitive external integration. We must wire in a real payment gateway
(Razorpay test-mode) without coupling the core to a network SDK, without
breaking the coverage discipline, and without ever risking a double-charge or a
false "recovered".

## Decision
Add `GatewayRecoveryExecutor`, implementing the Phase-7 `RecoveryExecutor`
protocol by charging through a `PaymentGateway` seam
(`charge(idempotency_key, amount_minor, currency) -> ChargeResult`).

- **Gateway injected.** Tests use deterministic fakes (`AlwaysPay`,
  `AlwaysDecline`, `Sequence`, `Raising`). The real backend
  `build_razorpay_gateway()` is a thin factory (lazy `import razorpay`,
  test-mode keys, `# pragma: no cover`), so the executor logic is 100%-testable
  offline and the core stays dependency-free.
- **End-to-end idempotency (G5).** The engine's deterministic key
  (`{leak.id}:attempt:{k}`) is passed straight to `gateway.charge` (as the
  Razorpay `receipt`/reference), so a retried recovery never double-charges.
- **Exact money conversion.** `money_to_minor_units` converts `Money` → integer
  paise and **rejects sub-unit amounts** (which would silently lose money) and
  non-positive amounts.
- **Safe degradation (G6).** A gateway that *raises* → `RecoveryError` → the
  engine HALTs for a human. A non-`ChargeResult` return → `RecoveryError`. The
  executor never fabricates a success.

## Consequences
- Recovery can now move real money in test-mode, while every non-network path
  is proven offline. Swapping providers = a new `PaymentGateway`; no core change.
- Running live requires the `razorpay` package + test-mode keys
  (`RAZORPAY_KEY_ID`/`_SECRET`); the engine's tests and CLI never need them.
- The test-mode `charge` creates an order keyed by the idempotency reference;
  a production capture/collect flow is a later refinement behind the same seam.

## Alternatives considered
- **Call the Razorpay SDK directly in the engine** — rejected (couples core to
  a network SDK; untestable offline).
- **Convert Money to float paise** — rejected (precision loss); integer
  minor-units with a rounded-amount guard instead.
- **Treat a gateway exception as a failed attempt** — rejected; an outage is not
  a decline, so it HALTs for a human rather than burning retries or misreporting.
