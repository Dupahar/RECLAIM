"""Sprint 1.1 tests — live payment executor (with deterministic fakes)."""
from datetime import datetime

import pytest

from reclaim.money import Money
from reclaim.domain import LeakRecord, LeakType, RecoveryState
from reclaim.recovery import AttemptResult, Channel, FailureReason, RecoveryEngine, RecoveryError
from reclaim.payments import (
    AlwaysDeclineGateway,
    AlwaysPayGateway,
    ChargeResult,
    GatewayRecoveryExecutor,
    PaymentError,
    RaisingGateway,
    SequenceGateway,
    money_to_minor_units,
)

TS = datetime(2026, 8, 26, 9, 0, 0)


def leak(amount="499.00"):
    return LeakRecord(id="L1", amount=Money.of(amount, "INR"), leak_type=LeakType.FAILED_DEBIT,
                      recoverable=True)


# --------------------------------------------------------------------------
# money_to_minor_units
# --------------------------------------------------------------------------
def test_minor_units_conversion():
    assert money_to_minor_units(Money.of("499.00", "INR")) == 49900
    assert money_to_minor_units(Money.of("1", "JPY")) == 1          # 0-dp currency
    assert money_to_minor_units(Money.of("1.00", "USD")) == 100


def test_minor_units_rejects_unrounded():
    with pytest.raises(PaymentError):
        money_to_minor_units(Money.of("1.234", "INR"))              # sub-paise -> reject


def test_minor_units_rejects_nonpositive_and_nonmoney():
    with pytest.raises(PaymentError):
        money_to_minor_units(Money.of("0", "INR"))
    with pytest.raises(PaymentError):
        money_to_minor_units(Money.of("-5.00", "INR"))
    with pytest.raises(PaymentError):
        money_to_minor_units(49900)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Executor result mapping
# --------------------------------------------------------------------------
def test_successful_charge_maps_to_succeeded():
    ex = GatewayRecoveryExecutor(AlwaysPayGateway())
    assert ex.attempt(leak(), Channel.UPI_RETRY, TS, "L1:attempt:0") is AttemptResult.SUCCEEDED


def test_declined_charge_maps_to_failed():
    ex = GatewayRecoveryExecutor(AlwaysDeclineGateway())
    assert ex.attempt(leak(), Channel.UPI_RETRY, TS, "L1:attempt:0") is AttemptResult.FAILED


def test_gateway_error_becomes_recovery_error():
    ex = GatewayRecoveryExecutor(RaisingGateway())
    with pytest.raises(RecoveryError):
        ex.attempt(leak(), Channel.UPI_RETRY, TS, "L1:attempt:0")


def test_non_chargeresult_return_is_recovery_error():
    class BadGateway:
        def charge(self, *a):
            return "ok"  # not a ChargeResult
    with pytest.raises(RecoveryError):
        GatewayRecoveryExecutor(BadGateway()).attempt(leak(), Channel.UPI_RETRY, TS, "k")


# --------------------------------------------------------------------------
# Idempotency key + amount are passed through to the gateway
# --------------------------------------------------------------------------
def test_idempotency_key_and_amount_passthrough():
    gw = SequenceGateway(outcomes=[True])
    GatewayRecoveryExecutor(gw).attempt(leak("499.00"), Channel.UPI_RETRY, TS, "L1:attempt:0")
    assert gw.calls == [("L1:attempt:0", 49900, "INR")]


# --------------------------------------------------------------------------
# Integration with the RecoveryEngine
# --------------------------------------------------------------------------
def test_engine_recovers_via_gateway():
    out = RecoveryEngine(GatewayRecoveryExecutor(AlwaysPayGateway())).recover(
        leak(), FailureReason.INSUFFICIENT_FUNDS, TS)
    assert out.final_state is RecoveryState.RECOVERED
    assert out.recovered_amount == Money.of("499.00", "INR")


def test_engine_exhausts_when_gateway_declines():
    out = RecoveryEngine(GatewayRecoveryExecutor(AlwaysDeclineGateway())).recover(
        leak(), FailureReason.INSUFFICIENT_FUNDS, TS)
    assert out.final_state is RecoveryState.EXHAUSTED


def test_engine_recovers_on_second_gateway_attempt():
    gw = SequenceGateway(outcomes=[False, True])   # decline then succeed
    out = RecoveryEngine(GatewayRecoveryExecutor(gw)).recover(
        leak(), FailureReason.SOFT_DECLINE, TS)
    assert out.final_state is RecoveryState.RECOVERED
    assert len(out.attempts) == 2
    # distinct idempotency keys per attempt
    assert [c[0] for c in gw.calls] == ["L1:attempt:0", "L1:attempt:1"]


def test_engine_halts_on_gateway_outage():
    out = RecoveryEngine(GatewayRecoveryExecutor(RaisingGateway())).recover(
        leak(), FailureReason.BANK_DOWNTIME, TS)
    assert out.final_state is RecoveryState.HALTED


def test_chargeresult_defaults():
    r = ChargeResult(succeeded=True)
    assert r.reference == ""


def test_sequence_gateway_exhaustion():
    gw = SequenceGateway(outcomes=[True])
    gw.charge("k0", 100, "INR")
    with pytest.raises(PaymentError):
        gw.charge("k1", 100, "INR")


def test_base_gateway_is_abstract():
    from reclaim.payments import PaymentGateway
    with pytest.raises(NotImplementedError):
        PaymentGateway().charge("k", 100, "INR")
