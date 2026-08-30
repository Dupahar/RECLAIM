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


# --------------------------------------------------------------------------
# Phase 21 — the pre-debit notice becomes an action, and the AFA ceiling
# --------------------------------------------------------------------------
from reclaim.recovery import AlwaysNotifies, NeverNotifies, RaisingNotifier


def test_notice_is_dispatched_before_any_debit():
    notifier = AlwaysNotifies()
    engine = RecoveryEngine(AlwaysSucceedsExecutor(), notice_executor=notifier)
    out = engine.recover(leak(), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.final_state is RecoveryState.RECOVERED
    assert out.notice_sent is True
    assert len(notifier.sent) == 1
    leak_id, at_time, key = notifier.sent[0]
    assert at_time == BASE                            # notice at t0...
    assert out.attempts[0].at_time == BASE + timedelta(hours=24)   # ...debit 24h later
    assert key == f"{leak_id}:notice"               # idempotent: one notice per leak


def test_without_a_notice_executor_the_window_is_only_modelled():
    """Honest reporting: no executor means no notice was actually sent."""
    out = RecoveryEngine(AlwaysSucceedsExecutor()).recover(
        leak(), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.final_state is RecoveryState.RECOVERED
    assert out.notice_at == BASE
    assert out.notice_sent is False


def test_rejected_notice_halts_before_debiting():
    engine = RecoveryEngine(AlwaysSucceedsExecutor(), notice_executor=NeverNotifies())
    out = engine.recover(leak(), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.final_state is RecoveryState.HALTED
    assert out.attempts == ()                       # nothing was charged
    assert "no debit without notice" in out.rationale


def test_notice_channel_failure_halts_before_debiting():
    engine = RecoveryEngine(AlwaysSucceedsExecutor(), notice_executor=RaisingNotifier())
    out = engine.recover(leak(), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.final_state is RecoveryState.HALTED
    assert out.attempts == ()
    assert out.recovered_amount is None


def test_amount_above_the_afa_ceiling_is_never_auto_debited():
    """RBI e-mandate: above the ceiling a debit needs customer authentication."""
    big = leak(amount="15000.01")
    out = RecoveryEngine(AlwaysSucceedsExecutor()).recover(
        big, FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.final_state is RecoveryState.HALTED
    assert out.attempts == ()
    assert "AFA-free ceiling" in out.rationale


def test_amount_at_the_afa_ceiling_is_still_allowed():
    out = RecoveryEngine(AlwaysSucceedsExecutor()).recover(
        leak(amount="15000.00"), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.final_state is RecoveryState.RECOVERED


def test_afa_ceiling_can_be_disabled_or_retuned():
    big = leak(amount="99999.00")
    off = RecoveryEngine(AlwaysSucceedsExecutor(), RecoveryConfig(afa_limit=None))
    assert off.recover(big, FailureReason.INSUFFICIENT_FUNDS, BASE).final_state is RecoveryState.RECOVERED
    tighter = RecoveryEngine(AlwaysSucceedsExecutor(),
                             RecoveryConfig(afa_limit=Money.of("100", "INR")))
    assert tighter.recover(leak(amount="500.00"), FailureReason.INSUFFICIENT_FUNDS,
                           BASE).final_state is RecoveryState.HALTED


def test_afa_ceiling_ignores_a_different_currency():
    """A rupee ceiling says nothing about a dollar debit — don't guess a rate."""
    usd = LeakRecord(id="L-usd", amount=Money.of("50000.00", "USD"),
                     leak_type=LeakType.SHORT_PAYMENT, source_refs=("s1",), recoverable=True)
    out = RecoveryEngine(AlwaysSucceedsExecutor()).recover(
        usd, FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.final_state is RecoveryState.RECOVERED


def test_afa_limit_validation():
    with pytest.raises(RecoveryError):
        RecoveryConfig(afa_limit="15000")
    with pytest.raises(RecoveryError):
        RecoveryConfig(afa_limit=Money.of("0", "INR"))


# --------------------------------------------------------------------------
# Phase 26 — the attempt scheduler seam (funded-moment re-timing)
# --------------------------------------------------------------------------
def test_the_first_attempt_can_be_retimed_and_later_ones_follow_it():
    """A re-timed sequence keeps its spacing rather than bunching back against
    the original fixed schedule."""
    predicted = BASE + timedelta(hours=24 + 96)          # 4 days past the deadline
    engine = RecoveryEngine(AlwaysFailsExecutor(),
                            RecoveryConfig(max_attempts=3, gap_hours=24),
                            scheduler=lambda l, deadline, fallback: predicted)
    out = engine.recover(leak("500.00"), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert [a.at_time for a in out.attempts] == [
        predicted, predicted + timedelta(hours=24), predicted + timedelta(hours=48)]


def test_a_retimed_success_says_so_in_its_rationale():
    predicted = BASE + timedelta(hours=48)
    engine = RecoveryEngine(AlwaysSucceedsExecutor(),
                            scheduler=lambda l, deadline, fallback: predicted)
    out = engine.recover(leak("500.00"), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.final_state is RecoveryState.RECOVERED
    assert "re-timed to 2026-08-27T09:00:00" in out.rationale


def test_a_scheduler_cannot_shorten_the_notice_window():
    """The compliance floor is not the timing model's to overrule. An answer at
    or before the deadline halts rather than being obeyed."""
    for proposed in (BASE, BASE + timedelta(hours=1), BASE + timedelta(hours=24)):
        engine = RecoveryEngine(AlwaysSucceedsExecutor(),
                                scheduler=lambda l, d, f, p=proposed: p)
        out = engine.recover(leak("500.00"), FailureReason.INSUFFICIENT_FUNDS, BASE)
        assert out.final_state is RecoveryState.HALTED
        assert out.attempts == ()
        assert "cannot shorten the notice window" in out.rationale


def test_a_scheduler_returning_nonsense_halts_rather_than_crashing():
    engine = RecoveryEngine(AlwaysSucceedsExecutor(),
                            scheduler=lambda l, d, f: "tomorrow-ish")
    out = engine.recover(leak("500.00"), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.final_state is RecoveryState.HALTED
    assert "non-datetime" in out.rationale


def test_without_a_scheduler_the_fixed_schedule_is_unchanged():
    """Regression guard: the seam must be invisible when unused."""
    out = RecoveryEngine(AlwaysFailsExecutor(), RecoveryConfig(max_attempts=2)).recover(
        leak("500.00"), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert [a.at_time for a in out.attempts] == [BASE + timedelta(hours=24),
                                                 BASE + timedelta(hours=48)]


def test_the_real_timing_module_drops_into_the_seam():
    """End to end with the actual predictor rather than a lambda."""
    from reclaim.timing import FundedMoment, fit as fit_timing, schedule_attempt

    history = [FundedMoment(customer_id="cust-1", at=datetime(2025, m, 2, 10))
               for m in range(1, 13)]
    predictor = fit_timing(history)
    engine = RecoveryEngine(
        AlwaysSucceedsExecutor(),
        scheduler=lambda l, deadline, fallback: schedule_attempt(
            predictor, "cust-1", notice_deadline=deadline, fallback=fallback).at)
    out = engine.recover(leak("500.00"), FailureReason.INSUFFICIENT_FUNDS, BASE)
    assert out.final_state is RecoveryState.RECOVERED
    # notice deadline is 2026-08-26T09:00; the next predicted funded moment is
    # the 2nd of September at 10:00
    assert out.attempts[0].at_time == datetime(2026, 9, 2, 10, 0, 0)
