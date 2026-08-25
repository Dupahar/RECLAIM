"""Live payment executor — a real gateway behind the recovery seam.

Implements the Phase-7 ``RecoveryExecutor`` protocol by charging (retrying) a
payment through a ``PaymentGateway`` seam. Same discipline as the LLM resolver:

- the gateway is **injected**, so the executor's logic (amount conversion,
  idempotency passthrough, result mapping, safe degradation) is 100%-testable
  with deterministic fakes, and
- the real Razorpay-test-mode client is a thin, clearly-marked factory
  (``build_razorpay_gateway``, ``# pragma: no cover`` — needs network + keys).

**Idempotency (goal G5):** the engine's deterministic idempotency key is passed
straight through to the gateway, so a retried recovery workflow never
double-charges. **Safe degradation (G6):** a gateway that *raises* becomes a
``RecoveryError``, which the ``RecoveryEngine`` treats as HALT-for-human — never
a silent double-debit or a false "recovered".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .money import Money
from .recovery import AttemptResult, Channel, RecoveryError


class PaymentError(Exception):
    """Raised by a PaymentGateway when a charge cannot be attempted."""


@dataclass(frozen=True)
class ChargeResult:
    succeeded: bool
    reference: str = ""


def money_to_minor_units(amount: Money) -> int:
    """Convert Money to an integer minor-unit amount (e.g. paise), safely.

    The amount must already be at the currency's minor-unit precision — a
    sub-unit amount would silently lose money in conversion, so we reject it.
    """
    if not isinstance(amount, Money):
        raise PaymentError("amount must be Money")
    if not amount.is_positive:
        raise PaymentError("charge amount must be positive")
    if not amount.is_rounded():
        raise PaymentError(f"amount {amount} is not at {amount.currency} minor-unit precision")
    scaled = amount.amount * (10 ** amount.minor_units)
    return int(scaled)


class PaymentGateway:
    """The seam a real payment provider implements."""

    def charge(self, idempotency_key: str, amount_minor: int, currency: str) -> ChargeResult:  # pragma: no cover - protocol
        raise NotImplementedError


class GatewayRecoveryExecutor:
    """A RecoveryExecutor that recovers money by charging through a gateway."""

    def __init__(self, gateway: PaymentGateway) -> None:
        self._gateway = gateway

    def attempt(self, leak, channel: Channel, at_time: datetime, idempotency_key: str) -> AttemptResult:
        amount_minor = money_to_minor_units(leak.amount)
        try:
            result = self._gateway.charge(idempotency_key, amount_minor, leak.amount.currency)
        except PaymentError as exc:
            # a gateway that cannot even attempt -> halt for human (never guess)
            raise RecoveryError(f"payment gateway error: {exc}") from exc
        if not isinstance(result, ChargeResult):
            raise RecoveryError("gateway returned a non-ChargeResult")
        return AttemptResult.SUCCEEDED if result.succeeded else AttemptResult.FAILED


# --------------------------------------------------------------------------
# Deterministic fake gateways — for tests and offline use.
# --------------------------------------------------------------------------
@dataclass
class AlwaysPayGateway:
    def charge(self, idempotency_key, amount_minor, currency) -> ChargeResult:
        return ChargeResult(succeeded=True, reference=f"pay_{idempotency_key}")


@dataclass
class AlwaysDeclineGateway:
    def charge(self, idempotency_key, amount_minor, currency) -> ChargeResult:
        return ChargeResult(succeeded=False)


@dataclass
class SequenceGateway:
    """Returns pre-set outcomes in order (e.g. decline then succeed)."""

    outcomes: list = field(default_factory=list)
    _i: int = 0
    calls: list = field(default_factory=list)  # records (key, amount_minor, currency) for assertions

    def charge(self, idempotency_key, amount_minor, currency) -> ChargeResult:
        self.calls.append((idempotency_key, amount_minor, currency))
        if self._i >= len(self.outcomes):
            raise PaymentError("SequenceGateway exhausted")
        ok = self.outcomes[self._i]
        self._i += 1
        return ChargeResult(succeeded=ok, reference=f"pay_{idempotency_key}" if ok else "")


@dataclass
class RaisingGateway:
    def charge(self, idempotency_key, amount_minor, currency) -> ChargeResult:
        raise PaymentError("simulated gateway outage")


def build_razorpay_gateway(key_id: str | None = None, key_secret: str | None = None):  # pragma: no cover - needs network/SDK/keys
    """Return a PaymentGateway backed by Razorpay **test-mode** APIs.

    Thin, network-dependent factory (not unit-tested). Requires the ``razorpay``
    package and test-mode credentials. The ``idempotency_key`` is used as the
    payment reference so retries are idempotent end to end.
    """
    import os
    import razorpay  # lazy import — keep the core dependency-free

    kid = key_id or os.environ["RAZORPAY_KEY_ID"]
    ksecret = key_secret or os.environ["RAZORPAY_KEY_SECRET"]
    client = razorpay.Client(auth=(kid, ksecret))

    class _RazorpayGateway:
        def charge(self, idempotency_key: str, amount_minor: int, currency: str) -> ChargeResult:
            # Test-mode: create an order keyed by the idempotency reference.
            order = client.order.create({
                "amount": amount_minor,
                "currency": currency,
                "receipt": idempotency_key,
                "notes": {"reclaim_recovery": idempotency_key},
            })
            return ChargeResult(succeeded=order.get("status") in ("created", "paid"),
                                reference=order.get("id", ""))

    return _RazorpayGateway()
