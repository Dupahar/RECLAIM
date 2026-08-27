"""Bounded recovery engine — the "reclaim" half of RECLAIM.

Acts on a recoverable leak: diagnoses the root cause, then runs a **bounded,
compliant** recovery workflow. Design mirrors the resolver's safety stance:

- **Deterministic orchestration.** Given the inputs (leak, failure reason,
  base time) and a deterministic executor, the outcome is reproducible (goal
  **G4**). No ``now()`` inside — the caller supplies ``base_time``.
- **Compliance & stopping rules are enforced, not optional.** The first debit
  attempt is scheduled only *after* the RBI-mandated 24-hour pre-debit notice;
  attempts are capped (``max_attempts``); each attempt carries a deterministic
  idempotency key so a retry never double-debits (goal **G5**).
- **The external effect is behind an interface.** A real UPI/gateway/WhatsApp
  action implements ``RecoveryExecutor``; tests use deterministic fakes.
- **Safe degradation (G6).** A permanent failure is *not chased*; an unknown
  cause or an executor error **halts for a human** — the engine never guesses.

Every outcome carries the full attempt log for the audit trail.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from .domain import LeakRecord, RecoveryState
from .money import Money


class RecoveryError(Exception):
    """Raised by a RecoveryExecutor to signal it could not act (e.g. rail down)."""


class FailureReason(str, Enum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    BANK_DOWNTIME = "bank_downtime"
    SOFT_DECLINE = "soft_decline"
    MANDATE_REVOKED = "mandate_revoked"
    HARD_DECLINE = "hard_decline"
    UNKNOWN = "unknown"


class RootCause(str, Enum):
    TEMPORARY = "temporary"      # recoverable — worth retrying
    PERMANENT = "permanent"      # not recoverable — do not chase
    UNKNOWN = "unknown"          # cannot classify — escalate to a human


class Channel(str, Enum):
    UPI_RETRY = "upi_retry"
    WHATSAPP_NUDGE = "whatsapp_nudge"


class AttemptResult(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_TEMPORARY = {FailureReason.INSUFFICIENT_FUNDS, FailureReason.BANK_DOWNTIME, FailureReason.SOFT_DECLINE}
_PERMANENT = {FailureReason.MANDATE_REVOKED, FailureReason.HARD_DECLINE}


def diagnose(reason: FailureReason) -> RootCause:
    """Rule-based root-cause classification (deterministic)."""
    if reason in _TEMPORARY:
        return RootCause.TEMPORARY
    if reason in _PERMANENT:
        return RootCause.PERMANENT
    return RootCause.UNKNOWN


@dataclass(frozen=True)
class RecoveryConfig:
    max_attempts: int = 3
    notice_hours: int = 24       # RBI mandatory pre-debit notice window
    gap_hours: int = 24          # spacing between successive attempts
    channels: tuple[Channel, ...] = (Channel.UPI_RETRY,)
    # RBI e-mandate: a recurring debit above this needs Additional Factor
    # Authentication, which is a customer action the engine cannot perform.
    # Above the ceiling the only correct autonomous behaviour is to stop and
    # hand over (G8). Set to None to disable the check.
    afa_limit: Optional[Money] = field(default_factory=lambda: Money.of("15000", "INR"))

    def __post_init__(self) -> None:
        for name, val in (("max_attempts", self.max_attempts),
                          ("notice_hours", self.notice_hours),
                          ("gap_hours", self.gap_hours)):
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                raise RecoveryError(f"{name} must be a non-negative int")
        if self.max_attempts < 1:
            raise RecoveryError("max_attempts must be >= 1")
        if not (isinstance(self.channels, tuple) and len(self.channels) >= 1
                and all(isinstance(c, Channel) for c in self.channels)):
            raise RecoveryError("channels must be a non-empty tuple of Channel")
        if self.afa_limit is not None:
            if not isinstance(self.afa_limit, Money):
                raise RecoveryError("afa_limit must be Money or None")
            if not self.afa_limit.is_positive:
                raise RecoveryError("afa_limit must be positive")


DEFAULT_CONFIG = RecoveryConfig()


@runtime_checkable
class NoticeExecutor(Protocol):
    """The seam that actually *sends* the RBI pre-debit notice.

    Recording a notice timestamp is not the same as giving notice. Until this
    existed the engine scheduled attempts after a 24-hour window it had merely
    written down; now the window is anchored to a notice that was really
    dispatched, and a notice that cannot be sent halts the recovery instead of
    silently debiting anyway.
    """

    def send(self, leak: LeakRecord, at_time: datetime,
             idempotency_key: str) -> bool:  # pragma: no cover - protocol
        ...


@runtime_checkable
class RecoveryExecutor(Protocol):
    """The seam a real UPI/gateway/WhatsApp action implements."""

    def attempt(self, leak: LeakRecord, channel: Channel, at_time: datetime,
                idempotency_key: str) -> AttemptResult:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class RecoveryAttempt:
    sequence: int
    channel: Channel
    at_time: datetime
    idempotency_key: str
    result: AttemptResult


@dataclass(frozen=True)
class RecoveryOutcome:
    leak: LeakRecord
    final_state: RecoveryState
    attempts: tuple[RecoveryAttempt, ...]
    recovered_amount: Optional[Money]
    rationale: str
    notice_at: Optional[datetime] = None
    notice_sent: bool = False     # True only when a NoticeExecutor confirmed dispatch


class RecoveryEngine:
    """Runs a bounded, compliant recovery workflow for one leak."""

    def __init__(self, executor: RecoveryExecutor, config: RecoveryConfig = DEFAULT_CONFIG,
                 notice_executor: "Optional[NoticeExecutor]" = None) -> None:
        self._executor = executor
        self._config = config
        self._notice_executor = notice_executor

    def recover(self, leak: LeakRecord, reason: FailureReason, base_time: datetime) -> RecoveryOutcome:
        if not isinstance(base_time, datetime):
            raise RecoveryError("base_time must be a datetime")
        cfg = self._config
        cause = diagnose(reason)

        if cause is RootCause.PERMANENT:
            return RecoveryOutcome(leak, RecoveryState.NOT_RECOVERABLE, (), None,
                                   f"permanent failure ({reason.value}); not chased", None)
        if cause is RootCause.UNKNOWN:
            return RecoveryOutcome(leak, RecoveryState.HALTED, (), None,
                                   f"unknown failure cause ({reason.value}); escalate to human", None)

        # TEMPORARY -> bounded, compliant retry sequence.
        limit = cfg.afa_limit
        if (limit is not None and leak.amount.currency == limit.currency
                and leak.amount > limit):
            return RecoveryOutcome(
                leak, RecoveryState.HALTED, (), None,
                f"amount {leak.amount} exceeds the AFA-free ceiling {limit}; "
                "a recurring debit this size needs customer authentication -- handed to a human",
                None)

        # The RBI pre-debit notice. With a NoticeExecutor this is dispatched for
        # real and the retry window starts from a notice that actually went out;
        # without one the window is only *modelled*, which the outcome records
        # honestly via notice_sent.
        notice_at = base_time
        notice_sent = False
        if self._notice_executor is not None:
            try:
                notice_sent = bool(self._notice_executor.send(
                    leak, notice_at, f"{leak.id}:notice"))
            except RecoveryError:
                return RecoveryOutcome(leak, RecoveryState.HALTED, (), None,
                                       "pre-debit notice could not be sent; halted for human "
                                       "(no debit without notice)", notice_at)
            if not notice_sent:
                return RecoveryOutcome(leak, RecoveryState.HALTED, (), None,
                                       "pre-debit notice was rejected; halted for human "
                                       "(no debit without notice)", notice_at)

        attempts: list[RecoveryAttempt] = []
        for k in range(cfg.max_attempts):
            at = base_time + timedelta(hours=cfg.notice_hours + k * cfg.gap_hours)
            channel = cfg.channels[k % len(cfg.channels)]
            key = f"{leak.id}:attempt:{k}"
            try:
                result = self._executor.attempt(leak, channel, at, key)
            except RecoveryError:
                return RecoveryOutcome(leak, RecoveryState.HALTED, tuple(attempts), None,
                                       "executor error; halted for human", notice_at,
                                       notice_sent)
            attempts.append(RecoveryAttempt(k, channel, at, key, result))
            if result is AttemptResult.SUCCEEDED:
                return RecoveryOutcome(leak, RecoveryState.RECOVERED, tuple(attempts),
                                       leak.amount, f"recovered on attempt {k + 1}", notice_at,
                                       notice_sent)

        return RecoveryOutcome(leak, RecoveryState.EXHAUSTED, tuple(attempts), None,
                               "all attempts failed; stopping rule reached", notice_at,
                               notice_sent)


# --------------------------------------------------------------------------
# Deterministic fake executors — for tests and offline defaults.
# --------------------------------------------------------------------------
@dataclass
class AlwaysNotifies:
    """Notice always goes out."""

    sent: list = field(default_factory=list)

    def send(self, leak, at_time, idempotency_key) -> bool:
        self.sent.append((leak.id, at_time, idempotency_key))
        return True


@dataclass
class NeverNotifies:
    """Notice is rejected — no debit may follow."""

    def send(self, leak, at_time, idempotency_key) -> bool:
        return False


@dataclass
class RaisingNotifier:
    """The notice channel itself is down."""

    def send(self, leak, at_time, idempotency_key) -> bool:
        raise RecoveryError("notice channel unavailable")


@dataclass
class AlwaysSucceedsExecutor:
    def attempt(self, leak, channel, at_time, idempotency_key) -> AttemptResult:
        return AttemptResult.SUCCEEDED


@dataclass
class AlwaysFailsExecutor:
    def attempt(self, leak, channel, at_time, idempotency_key) -> AttemptResult:
        return AttemptResult.FAILED


@dataclass
class SequenceExecutor:
    """Returns pre-set results in order (to simulate 'succeeds on attempt 2')."""

    results: list = field(default_factory=list)
    _i: int = 0

    def attempt(self, leak, channel, at_time, idempotency_key) -> AttemptResult:
        if self._i >= len(self.results):
            raise RecoveryError("SequenceExecutor exhausted")
        r = self.results[self._i]
        self._i += 1
        return r


@dataclass
class RaisingExecutor:
    def attempt(self, leak, channel, at_time, idempotency_key) -> AttemptResult:
        raise RecoveryError("simulated rail failure")
