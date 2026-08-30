"""Conduct guardrails — consent, quiet hours, and caps that survive a restart.

Architecture §7: *"Hard **stopping rules** (contact caps, RBI 24h notice,
consent) are enforced in the workflow, not left to a model."* Two of those three
were enforced. This is the third, plus the part of the first that was missing.

**The gap this closes is embarrassing when stated plainly.** `max_attempts` caps
attempts *within a single recovery run*. Nothing capped contacts *across* runs.
Re-run yesterday's batch today and the same customer is contacted again, three
more times, and the engine cannot see that it has done so. A daily cron would
have produced textbook harassment while every in-process invariant held. The cap
has to live in durable state or it is not a cap.

**Consent was not modelled at all**, which is worse, because the failure is
silent: an engine that has never heard of consent behaves identically to one
whose customers have all granted it.

    consent -> quiet hours -> cooling-off -> window cap -> allowed

**Default deny.** An unknown consent state refuses. This is the single most
important line in the module: "we have no record" and "they said yes" must not
produce the same behaviour, and the direction of the default decides which one a
missing row silently becomes.

**Withdrawal is permanent and retroactive to the moment it was recorded.**
Consent is stored append-only and read *as of* a timestamp, so a replay of last
week's run sees last week's consent — not today's. A guardrail that re-decides
history with today's data cannot be used to audit what was actually allowed.

**Replay must not consume the cap.** Contacts are idempotent by idempotency key,
the same key the payment attempt carries. Re-persisting a run therefore neither
double-counts a contact nor exhausts a customer's allowance — the same rule the
money ledger and the audit log already apply.

**Every refusal names the rule that produced it.** A `Ruling` carries the rule
id, so a customer-facing "why did you not chase this?" has one answer rather than
an inference, and the HITL queue can show it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from .recovery import Channel


class ConductError(Exception):
    """Raised on an invalid conduct configuration or record."""


class ConsentState(str, Enum):
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"
    UNKNOWN = "unknown"      # never recorded — refuses, by design


# Rule ids, so a refusal is machine-readable as well as human-readable.
RULE_CONSENT = "consent"
RULE_QUIET_HOURS = "quiet_hours"
RULE_COOLING_OFF = "cooling_off"
RULE_WINDOW_CAP = "window_cap"
RULE_ALLOWED = "allowed"


@dataclass(frozen=True)
class ConsentGrant:
    """One recorded consent event. Append-only; never edited."""

    customer_id: str
    state: ConsentState
    at: datetime
    source: str = ""
    expires_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not (isinstance(self.customer_id, str) and self.customer_id):
            raise ConductError("customer_id is required")
        if not isinstance(self.state, ConsentState):
            raise ConductError("state must be a ConsentState")
        if self.state is ConsentState.UNKNOWN:
            raise ConductError("UNKNOWN is the absence of a record, not a record; "
                               "do not store it")
        if not isinstance(self.at, datetime):
            raise ConductError("at must be a datetime")
        if self.expires_at is not None:
            if not isinstance(self.expires_at, datetime):
                raise ConductError("expires_at must be a datetime or None")
            if self.expires_at <= self.at:
                raise ConductError("expires_at must be after the grant")
        if self.state is not ConsentState.GRANTED and self.expires_at is not None:
            raise ConductError("only a GRANTED consent can expire")


class ConsentRegistry:
    """Append-only consent history, readable as of any point in time."""

    def __init__(self) -> None:
        self._grants: dict[str, list[ConsentGrant]] = {}

    def record(self, grant: ConsentGrant) -> ConsentGrant:
        if not isinstance(grant, ConsentGrant):
            raise ConductError("record() requires a ConsentGrant")
        history = self._grants.setdefault(grant.customer_id, [])
        if grant in history:
            return grant                      # identical replay
        history.append(grant)
        # Kept sorted by time so `state_at` is a scan backwards, and so an
        # out-of-order import still reads correctly.
        history.sort(key=lambda g: g.at)
        return grant

    def history(self, customer_id: str) -> tuple[ConsentGrant, ...]:
        return tuple(self._grants.get(customer_id, ()))

    def customers(self) -> tuple[str, ...]:
        """Every customer with a recorded grant — what a repository iterates."""
        return tuple(self._grants)

    def state_at(self, customer_id: str, at: datetime) -> ConsentState:
        """Consent as it stood at ``at`` — not as it stands now.

        Reading the current state would make a replay of an old run judge it by
        today's consent, which is a different question from "was this allowed
        when we did it?".
        """
        if not isinstance(at, datetime):
            raise ConductError("at must be a datetime")
        latest = None
        for grant in self._grants.get(customer_id, ()):
            if grant.at <= at:
                latest = grant
        if latest is None:
            return ConsentState.UNKNOWN
        if latest.state is ConsentState.GRANTED and latest.expires_at is not None:
            if at >= latest.expires_at:
                return ConsentState.EXPIRED
        return latest.state

    @property
    def size(self) -> int:
        return len(self._grants)


@dataclass(frozen=True)
class ContactRecord:
    """One contact actually made. The unit of the cross-run cap."""

    customer_id: str
    unit_id: str
    channel: Channel
    at: datetime
    idempotency_key: str

    def __post_init__(self) -> None:
        for name, val in (("customer_id", self.customer_id), ("unit_id", self.unit_id),
                          ("idempotency_key", self.idempotency_key)):
            if not (isinstance(val, str) and val):
                raise ConductError(f"{name} is required")
        if not isinstance(self.channel, Channel):
            raise ConductError("channel must be a Channel")
        if not isinstance(self.at, datetime):
            raise ConductError("at must be a datetime")


class ContactLedger:
    """Durable, idempotent record of who has been contacted and when.

    Idempotent by ``idempotency_key`` — the same key the payment attempt carries —
    so replaying a run does not consume a customer's allowance.
    """

    def __init__(self) -> None:
        self._by_customer: dict[str, list[ContactRecord]] = {}
        self._keys: set[str] = set()

    def record(self, contact: ContactRecord) -> bool:
        """Append a contact. Returns False if this key was already recorded."""
        if not isinstance(contact, ContactRecord):
            raise ConductError("record() requires a ContactRecord")
        if contact.idempotency_key in self._keys:
            return False
        self._keys.add(contact.idempotency_key)
        history = self._by_customer.setdefault(contact.customer_id, [])
        history.append(contact)
        history.sort(key=lambda c: (c.at, c.idempotency_key))
        return True

    def contacts_for(self, customer_id: str) -> tuple[ContactRecord, ...]:
        return tuple(self._by_customer.get(customer_id, ()))

    def customers(self) -> tuple[str, ...]:
        """Every customer with a recorded contact — what a repository iterates."""
        return tuple(self._by_customer)

    def count_since(self, customer_id: str, since: datetime, until: datetime) -> int:
        """Contacts in the half-open window ``(since, until]``."""
        return sum(1 for c in self._by_customer.get(customer_id, ())
                   if since < c.at <= until)

    def last_contact(self, customer_id: str, before: datetime) -> Optional[ContactRecord]:
        latest = None
        for contact in self._by_customer.get(customer_id, ()):
            if contact.at <= before:
                latest = contact
        return latest

    @property
    def size(self) -> int:
        return len(self._keys)


@dataclass(frozen=True)
class ConductPolicy:
    """The hard stopping rules, as configuration rather than scattered literals.

    Defaults are deliberately conservative. A recovery product's failure mode is
    not "too few contacts"; it is a merchant's customer receiving a fourth debit
    notice in a week and never using that merchant again.
    """

    max_contacts_per_window: int = 3
    window_days: int = 30
    min_hours_between: int = 24
    # Local hours in which no contact may be made. Wraps midnight when the start
    # is after the end. ``None`` disables the rule.
    quiet_hours: Optional[tuple[int, int]] = (21, 8)
    require_consent: bool = True

    def __post_init__(self) -> None:
        for name, val in (("max_contacts_per_window", self.max_contacts_per_window),
                          ("window_days", self.window_days),
                          ("min_hours_between", self.min_hours_between)):
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                raise ConductError(f"{name} must be a non-negative int")
        if self.max_contacts_per_window < 1:
            raise ConductError("max_contacts_per_window must be >= 1")
        if self.window_days < 1:
            raise ConductError("window_days must be >= 1")
        if not isinstance(self.require_consent, bool):
            raise ConductError("require_consent must be a bool")
        if self.quiet_hours is not None:
            if not (isinstance(self.quiet_hours, tuple) and len(self.quiet_hours) == 2):
                raise ConductError("quiet_hours must be a (start, end) tuple or None")
            for hour in self.quiet_hours:
                if not isinstance(hour, int) or isinstance(hour, bool) or not (0 <= hour <= 23):
                    raise ConductError("quiet_hours must be hours within 0..23")
            if self.quiet_hours[0] == self.quiet_hours[1]:
                raise ConductError("quiet_hours start and end must differ; use None to "
                                   "disable rather than a zero-width window")

    def is_quiet(self, at: datetime) -> bool:
        if self.quiet_hours is None:
            return False
        start, end = self.quiet_hours
        if start < end:
            return start <= at.hour < end
        return at.hour >= start or at.hour < end     # wraps midnight


DEFAULT_POLICY = ConductPolicy()


@dataclass(frozen=True)
class Ruling:
    """May we contact this customer, and which rule decided?"""

    allowed: bool
    rule: str
    reason: str

    def summary(self) -> dict:
        return {"allowed": self.allowed, "rule": self.rule, "reason": self.reason}


def may_contact(customer_id: str, at: datetime, *,
                consent: Optional[ConsentRegistry] = None,
                contacts: Optional[ContactLedger] = None,
                policy: ConductPolicy = DEFAULT_POLICY) -> Ruling:
    """Apply the stopping rules, in the order they should be applied.

    Order is deliberate. Consent comes first because it is the only rule whose
    violation is a wrong done to a person rather than a policy breach — no cap or
    quiet-hour argument can make contacting someone who withdrew acceptable.
    Quiet hours next, because a contact at 3am is harmful whatever the counts
    say. Then the cooling-off period, then the window cap, which is the
    narrowest.

    Every registry is optional so the gate can be adopted incrementally, but the
    defaults are the safe ones: with ``require_consent`` on and no registry
    supplied, every contact is refused rather than waved through.
    """
    if not (isinstance(customer_id, str) and customer_id):
        raise ConductError("customer_id is required")
    if not isinstance(at, datetime):
        raise ConductError("at must be a datetime")

    if policy.require_consent:
        state = (consent.state_at(customer_id, at) if consent is not None
                 else ConsentState.UNKNOWN)
        if state is not ConsentState.GRANTED:
            return Ruling(False, RULE_CONSENT,
                          f"consent is {state.value} as of {at.isoformat()}; "
                          "no record and a refusal must not look the same, so the "
                          "default is deny")

    if policy.is_quiet(at):
        start, end = policy.quiet_hours
        return Ruling(False, RULE_QUIET_HOURS,
                      f"{at.hour:02d}:00 falls in quiet hours "
                      f"{start:02d}:00-{end:02d}:00")

    if contacts is not None:
        previous = contacts.last_contact(customer_id, at)
        if previous is not None and policy.min_hours_between > 0:
            earliest = previous.at + timedelta(hours=policy.min_hours_between)
            if at < earliest:
                return Ruling(False, RULE_COOLING_OFF,
                              f"last contacted {previous.at.isoformat()}; the "
                              f"{policy.min_hours_between}h cooling-off period runs to "
                              f"{earliest.isoformat()}")

        window_start = at - timedelta(days=policy.window_days)
        used = contacts.count_since(customer_id, window_start, at)
        if used >= policy.max_contacts_per_window:
            return Ruling(False, RULE_WINDOW_CAP,
                          f"{used} contact/s already made in the last "
                          f"{policy.window_days} days, cap is "
                          f"{policy.max_contacts_per_window}")

    return Ruling(True, RULE_ALLOWED, "no stopping rule applies")


# --------------------------------------------------------------------------
# The gate the recovery engine calls
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ConductGate:
    """Bundles the registries and the policy into one callable for the engine.

    Recording is the caller's job on purpose: this object answers "may we?", and
    something that both authorises and records would make a dry run indistinguishable
    from a real one.
    """

    consent: Optional[ConsentRegistry] = None
    contacts: Optional[ContactLedger] = None
    policy: ConductPolicy = DEFAULT_POLICY
    # How to get a customer id from a leak. Defaults to the leak's own
    # ``customer_ref`` -- carried from the settlement's counterparty by
    # reconciliation -- so caps are per-customer without any wiring. Falls back
    # to the leak id only when the source carried no counterparty, which makes
    # the cap per-leak; that is weaker, and ``is_per_customer`` reports it rather
    # than letting it pass for the real thing.
    customer_for: Optional[object] = None

    def customer_id(self, leak) -> str:
        if self.customer_for is not None:
            return self.customer_for(leak)
        return leak.customer_ref if leak.customer_ref else leak.id

    def is_per_customer(self, leak) -> bool:
        """False when the cap for this leak degrades to per-leak scope."""
        if self.customer_for is not None:
            return True
        return bool(leak.customer_ref)

    def __call__(self, leak, at: datetime) -> Ruling:
        return may_contact(self.customer_id(leak), at, consent=self.consent,
                           contacts=self.contacts, policy=self.policy)

    def note(self, leak, channel: Channel, at: datetime, idempotency_key: str) -> bool:
        """Record a contact that was actually made. Idempotent by key."""
        if self.contacts is None:
            return False
        return self.contacts.record(ContactRecord(
            customer_id=self.customer_id(leak), unit_id=leak.id, channel=channel,
            at=at, idempotency_key=idempotency_key))
