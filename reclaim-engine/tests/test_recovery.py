"""Phase 7 tests — bounded recovery engine."""
from datetime import datetime, timedelta

import pytest

from reclaim.money import Money
from reclaim.domain import LeakRecord, LeakType, RecoveryState
from reclaim.recovery import (
    AlwaysFailsExecutor,
    AlwaysSucceedsExecutor,
    AttemptResult,
    Channel,
    FailureReason,
    RaisingExecutor,
    RecoveryConfig,
    RecoveryEngine,
    RecoveryError,
    RootCause,
    SequenceExecutor,
    diagnose,
)

BASE = datetime(2026, 8, 25, 9, 0, 0)


def leak(amount="499.00"):
    return LeakRecord(id="L1", amount=Money.of(amount, "INR"), leak_type=LeakType.FAILED_DEBIT,
                      recoverable=True)


# --------------------------------------------------------------------------
# Diagnosis
# --------------------------------------------------------------------------
@pytest.mark.parametrize("reason,expected", [
    (FailureReason.INSUFFICIENT_FUNDS, RootCause.TEMPORARY),
    (FailureReason.BANK_DOWNTIME, RootCause.TEMPORARY),
    (FailureReason.SOFT_DECLINE, RootCause.TEMPORARY),
    (FailureReason.MANDATE_REVOKED, RootCause.PERMANENT),
    (FailureReason.HARD_DECLINE, RootCause.PERMANENT),
    (FailureReason.UNKNOWN, RootCause.UNKNOWN),
])
def test_diagnose(reason, expected):
    assert diagnose(reason) == expected


# --------------------------------------------------------------------------
# Config validation
# --------------------------------------------------------------------------
def test_config_validation():
    with pytest.raises(RecoveryError):
        RecoveryConfig(max_attempts=0)
    with pytest.raises(RecoveryError):
        RecoveryConfig(notice_hours=-1)
    with pytest.raises(RecoveryError):
        RecoveryConfig(max_attempts=True)  # type: ignore[arg-type]
    with pytest.raises(RecoveryError):
        RecoveryConfig(channels=())
    with pytest.raises(RecoveryError):
        RecoveryConfig(channels=("upi",))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Diagnosis-driven outcomes
# --------------------------------------------------------------------------
def test_permanent_not_chased():
    out = RecoveryEngine(AlwaysSucceedsExecutor()).recover(leak(), FailureReason.MANDATE_REVOKED, BASE)
    assert out.final_state is RecoveryState.NOT_RECOVERABLE
    assert out.attempts == ()          # never attempted — no harassment
    assert out.recovered_amount is None


def test_unknown_halts_for_human():
    out = RecoveryEngine(AlwaysSucceedsExecutor()).recover(leak(), FailureReason.UNKNOWN, BASE)
    assert out.final_state is RecoveryState.HALTED
    assert out.attempts == ()


# --------------------------------------------------------------------------
# Temporary — success / retry / exhaustion
# --------------------------------------------------------------------------
def test_recovered_on_first_attempt_respects_notice_window():
    out = RecoveryEngine(AlwaysSucceedsExecutor()).recover(leak(), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.final_state is RecoveryState.RECOVERED
    assert out.recovered_amount == Money.of("499.00", "INR")
    assert len(out.attempts) == 1
    # first attempt only after the 24h pre-debit notice
    assert out.notice_at == BASE
    assert out.attempts[0].at_time == BASE + timedelta(hours=24)
    assert out.attempts[0].idempotency_key == "L1:attempt:0"


def test_recovered_on_second_attempt():
    execu = SequenceExecutor([AttemptResult.FAILED, AttemptResult.SUCCEEDED])
    out = RecoveryEngine(execu).recover(leak(), FailureReason.SOFT_DECLINE, BASE)
    assert out.final_state is RecoveryState.RECOVERED
    assert len(out.attempts) == 2
    assert out.attempts[1].at_time == BASE + timedelta(hours=48)  # 24 + 1*24


def test_exhausted_hits_stopping_rule():
    out = RecoveryEngine(AlwaysFailsExecutor()).recover(leak(), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.final_state is RecoveryState.EXHAUSTED
    assert len(out.attempts) == 3       # capped at max_attempts
    assert out.recovered_amount is None


def test_executor_error_halts():
    out = RecoveryEngine(RaisingExecutor()).recover(leak(), FailureReason.BANK_DOWNTIME, BASE)
    assert out.final_state is RecoveryState.HALTED
    assert "executor error" in out.rationale


# --------------------------------------------------------------------------
# Channels, stopping-rule bound, determinism
# --------------------------------------------------------------------------
def test_multi_channel_rotation():
    cfg = RecoveryConfig(channels=(Channel.UPI_RETRY, Channel.WHATSAPP_NUDGE))
    out = RecoveryEngine(AlwaysFailsExecutor(), cfg).recover(leak(), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.attempts[0].channel is Channel.UPI_RETRY
    assert out.attempts[1].channel is Channel.WHATSAPP_NUDGE
    assert out.attempts[2].channel is Channel.UPI_RETRY  # rotates


def test_max_attempts_config_respected():
    cfg = RecoveryConfig(max_attempts=1)
    out = RecoveryEngine(AlwaysFailsExecutor(), cfg).recover(leak(), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert len(out.attempts) == 1
    assert out.final_state is RecoveryState.EXHAUSTED


def test_determinism():
    a = RecoveryEngine(AlwaysFailsExecutor()).recover(leak(), FailureReason.INSUFFICIENT_FUNDS, BASE)
    b = RecoveryEngine(AlwaysFailsExecutor()).recover(leak(), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert a == b


def test_base_time_must_be_datetime():
    with pytest.raises(RecoveryError):
        RecoveryEngine(AlwaysSucceedsExecutor()).recover(leak(), FailureReason.SOFT_DECLINE, "2026-08-25")  # type: ignore[arg-type]


def test_sequence_executor_exhaustion():
    ex = SequenceExecutor([AttemptResult.FAILED])
    ex.attempt(None, Channel.UPI_RETRY, BASE, "k0")
    with pytest.raises(RecoveryError):
        ex.attempt(None, Channel.UPI_RETRY, BASE, "k1")
