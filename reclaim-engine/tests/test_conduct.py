"""Phase 28 tests — conduct guardrails (architecture §7, hard stopping rules).

The gap being closed is that `max_attempts` capped attempts within one run and
nothing capped contacts across runs, so a daily cron would have produced textbook
harassment while every in-process invariant held. The tests that matter are
therefore the ones that span runs, and the ones that pin default-deny.
"""
from datetime import datetime, timedelta

import pytest

from reclaim.conduct import (
    DEFAULT_POLICY,
    RULE_ALLOWED,
    RULE_CONSENT,
    RULE_COOLING_OFF,
    RULE_QUIET_HOURS,
    RULE_WINDOW_CAP,
    ConductError,
    ConductGate,
    ConductPolicy,
    ConsentGrant,
    ConsentRegistry,
    ConsentState,
    ContactLedger,
    ContactRecord,
    Ruling,
    may_contact,
)
from reclaim.domain import LeakRecord, LeakType
from reclaim.money import Money
from reclaim.recovery import Channel

TS = datetime(2026, 8, 25, 10, 0, 0)


def leak(lid="leak:1", customer_ref=None):
    return LeakRecord(id=lid, amount=Money.of("500", "INR"),
                      leak_type=LeakType.SHORT_PAYMENT, recoverable=True,
                      customer_ref=customer_ref)


def granted(customer="cust-1", at=None, **kw):
    return ConsentGrant(customer, ConsentState.GRANTED, at or TS - timedelta(days=1),
                        **kw)


def contact(customer="cust-1", at=None, key="k1", channel=Channel.UPI_RETRY):
    return ContactRecord(customer_id=customer, unit_id="leak:1", channel=channel,
                         at=at or TS, idempotency_key=key)


def allowing(customer="cust-1"):
    """A registry with consent in place, so other rules can be tested alone."""
    registry = ConsentRegistry()
    registry.record(granted(customer))
    return registry


# --------------------------------------------------------------------------
# Consent records
# --------------------------------------------------------------------------
def test_consent_grant_validates_itself():
    with pytest.raises(ConductError):
        ConsentGrant("", ConsentState.GRANTED, TS)
    with pytest.raises(ConductError):
        ConsentGrant("c1", "granted", TS)
    with pytest.raises(ConductError):
        ConsentGrant("c1", ConsentState.GRANTED, "2026-08-25")
    with pytest.raises(ConductError):
        granted(expires_at="2026-09-01")
    with pytest.raises(ConductError):
        granted(at=TS, expires_at=TS)                       # must be after
    with pytest.raises(ConductError):
        ConsentGrant("c1", ConsentState.WITHDRAWN, TS, expires_at=TS + timedelta(days=1))


def test_unknown_is_the_absence_of_a_record_not_a_record():
    """Storing UNKNOWN would let "we asked and they didn't answer" masquerade as
    a decision. It is refused at construction."""
    with pytest.raises(ConductError) as exc:
        ConsentGrant("c1", ConsentState.UNKNOWN, TS)
    assert "absence of a record" in str(exc.value)


def test_registry_rejects_a_non_grant():
    with pytest.raises(ConductError):
        ConsentRegistry().record("granted")


def test_recording_the_same_grant_twice_is_a_no_op():
    registry = ConsentRegistry()
    registry.record(granted())
    registry.record(granted())
    assert len(registry.history("cust-1")) == 1
    assert registry.size == 1


def test_history_is_kept_in_time_order_even_if_imported_out_of_order():
    registry = ConsentRegistry()
    late = ConsentGrant("cust-1", ConsentState.WITHDRAWN, TS + timedelta(days=5))
    registry.record(late)
    registry.record(granted(at=TS))
    assert [g.at for g in registry.history("cust-1")] == [TS, late.at]


# --------------------------------------------------------------------------
# Consent as of a point in time
# --------------------------------------------------------------------------
def test_no_record_reads_as_unknown():
    assert ConsentRegistry().state_at("nobody", TS) is ConsentState.UNKNOWN


def test_state_at_validates_its_timestamp():
    with pytest.raises(ConductError):
        ConsentRegistry().state_at("c1", "2026-08-25")


def test_consent_is_read_as_of_the_moment_not_as_it_stands_now():
    """A replay of last week's run must see last week's consent. A guardrail
    that re-decides history with today's data cannot audit what was allowed."""
    registry = ConsentRegistry()
    registry.record(granted(at=TS))
    registry.record(ConsentGrant("cust-1", ConsentState.WITHDRAWN, TS + timedelta(days=3)))
    assert registry.state_at("cust-1", TS - timedelta(days=1)) is ConsentState.UNKNOWN
    assert registry.state_at("cust-1", TS + timedelta(days=1)) is ConsentState.GRANTED
    assert registry.state_at("cust-1", TS + timedelta(days=5)) is ConsentState.WITHDRAWN


def test_an_expired_grant_reads_as_expired_but_only_after_it_expires():
    registry = ConsentRegistry()
    registry.record(granted(at=TS, expires_at=TS + timedelta(days=30)))
    assert registry.state_at("cust-1", TS + timedelta(days=29)) is ConsentState.GRANTED
    assert registry.state_at("cust-1", TS + timedelta(days=30)) is ConsentState.EXPIRED
    assert registry.state_at("cust-1", TS + timedelta(days=99)) is ConsentState.EXPIRED


def test_a_withdrawal_does_not_expire_back_into_consent():
    registry = ConsentRegistry()
    registry.record(granted(at=TS, expires_at=TS + timedelta(days=10)))
    registry.record(ConsentGrant("cust-1", ConsentState.WITHDRAWN, TS + timedelta(days=1)))
    assert registry.state_at("cust-1", TS + timedelta(days=99)) is ConsentState.WITHDRAWN


# --------------------------------------------------------------------------
# The contact ledger
# --------------------------------------------------------------------------
def test_contact_record_validates_itself():
    with pytest.raises(ConductError):
        ContactRecord("", "leak:1", Channel.UPI_RETRY, TS, "k")
    with pytest.raises(ConductError):
        ContactRecord("c1", "", Channel.UPI_RETRY, TS, "k")
    with pytest.raises(ConductError):
        ContactRecord("c1", "leak:1", Channel.UPI_RETRY, TS, "")
    with pytest.raises(ConductError):
        ContactRecord("c1", "leak:1", "upi", TS, "k")
    with pytest.raises(ConductError):
        ContactRecord("c1", "leak:1", Channel.UPI_RETRY, "now", "k")


def test_ledger_rejects_a_non_record():
    with pytest.raises(ConductError):
        ContactLedger().record("contacted")


def test_replaying_a_contact_does_not_consume_the_allowance():
    """The property that makes the cap safe under re-persist: idempotent by the
    same key the payment attempt carries."""
    ledger = ContactLedger()
    assert ledger.record(contact(key="leak:1:attempt:0")) is True
    assert ledger.record(contact(key="leak:1:attempt:0")) is False
    assert ledger.size == 1
    assert len(ledger.contacts_for("cust-1")) == 1


def test_counting_is_over_a_half_open_window():
    ledger = ContactLedger()
    for i in range(3):
        ledger.record(contact(at=TS + timedelta(days=i), key=f"k{i}"))
    assert ledger.count_since("cust-1", TS - timedelta(days=1), TS + timedelta(days=2)) == 3
    assert ledger.count_since("cust-1", TS, TS + timedelta(days=2)) == 2   # excludes TS
    assert ledger.count_since("nobody", TS, TS + timedelta(days=9)) == 0


def test_last_contact_respects_the_bound():
    ledger = ContactLedger()
    ledger.record(contact(at=TS, key="a"))
    ledger.record(contact(at=TS + timedelta(days=2), key="b"))
    assert ledger.last_contact("cust-1", TS + timedelta(days=1)).idempotency_key == "a"
    assert ledger.last_contact("cust-1", TS + timedelta(days=3)).idempotency_key == "b"
    assert ledger.last_contact("cust-1", TS - timedelta(days=1)) is None
    assert ledger.last_contact("nobody", TS) is None


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------
def test_policy_validates_its_numbers():
    for bad in ({"max_contacts_per_window": 0}, {"max_contacts_per_window": -1},
                {"window_days": 0}, {"min_hours_between": -1},
                {"min_hours_between": True}, {"require_consent": "yes"}):
        with pytest.raises(ConductError):
            ConductPolicy(**bad)


def test_policy_validates_quiet_hours():
    for bad in ([21, 8], (21,), (21, 8, 9), (21, 24), (-1, 8), (True, 8), (9, 9)):
        with pytest.raises(ConductError):
            ConductPolicy(quiet_hours=bad)


def test_quiet_hours_wrap_midnight():
    policy = ConductPolicy(quiet_hours=(21, 8))
    assert policy.is_quiet(TS.replace(hour=22)) is True
    assert policy.is_quiet(TS.replace(hour=3)) is True
    assert policy.is_quiet(TS.replace(hour=8)) is False       # end is exclusive
    assert policy.is_quiet(TS.replace(hour=20)) is False


def test_quiet_hours_can_also_be_a_daytime_window():
    policy = ConductPolicy(quiet_hours=(13, 15))
    assert policy.is_quiet(TS.replace(hour=13)) is True
    assert policy.is_quiet(TS.replace(hour=15)) is False
    assert policy.is_quiet(TS.replace(hour=2)) is False


def test_quiet_hours_can_be_disabled():
    assert ConductPolicy(quiet_hours=None).is_quiet(TS.replace(hour=3)) is False
    assert DEFAULT_POLICY.quiet_hours == (21, 8)


# --------------------------------------------------------------------------
# may_contact — default deny, and the rule order
# --------------------------------------------------------------------------
def test_may_contact_validates_its_arguments():
    with pytest.raises(ConductError):
        may_contact("", TS)
    with pytest.raises(ConductError):
        may_contact("cust-1", "2026-08-25")


def test_with_no_consent_registry_at_all_every_contact_is_refused():
    """The direction of the default decides what a missing row silently becomes.
    It becomes a refusal."""
    ruling = may_contact("cust-1", TS)
    assert ruling.allowed is False and ruling.rule == RULE_CONSENT
    assert "default is deny" in ruling.reason


def test_an_unknown_customer_is_refused_even_with_a_registry_present():
    ruling = may_contact("stranger", TS, consent=allowing())
    assert ruling.allowed is False and ruling.rule == RULE_CONSENT


def test_withdrawn_and_expired_consent_are_both_refusals():
    withdrawn = ConsentRegistry()
    withdrawn.record(ConsentGrant("cust-1", ConsentState.WITHDRAWN, TS - timedelta(days=1)))
    assert may_contact("cust-1", TS, consent=withdrawn).rule == RULE_CONSENT

    expired = ConsentRegistry()
    expired.record(granted(at=TS - timedelta(days=40), expires_at=TS - timedelta(days=1)))
    assert may_contact("cust-1", TS, consent=expired).rule == RULE_CONSENT


def test_consent_can_be_switched_off_for_a_deployment_that_gates_it_elsewhere():
    policy = ConductPolicy(require_consent=False)
    assert may_contact("cust-1", TS, policy=policy).allowed is True


def test_consent_outranks_every_other_rule():
    """No cap or quiet-hour argument makes contacting someone who withdrew
    acceptable, so consent is checked first."""
    withdrawn = ConsentRegistry()
    withdrawn.record(ConsentGrant("cust-1", ConsentState.WITHDRAWN, TS - timedelta(days=1)))
    at_3am = TS.replace(hour=3)
    ruling = may_contact("cust-1", at_3am, consent=withdrawn, contacts=ContactLedger())
    assert ruling.rule == RULE_CONSENT       # not quiet_hours


def test_a_contact_in_quiet_hours_is_refused():
    ruling = may_contact("cust-1", TS.replace(hour=2), consent=allowing())
    assert ruling.allowed is False and ruling.rule == RULE_QUIET_HOURS
    assert "21:00-08:00" in ruling.reason


def test_the_cooling_off_period_is_enforced():
    ledger = ContactLedger()
    ledger.record(contact(at=TS, key="a"))
    ruling = may_contact("cust-1", TS + timedelta(hours=6), consent=allowing(),
                         contacts=ledger)
    assert ruling.allowed is False and ruling.rule == RULE_COOLING_OFF
    assert "cooling-off period runs to" in ruling.reason
    later = may_contact("cust-1", TS + timedelta(hours=25), consent=allowing(),
                        contacts=ledger)
    assert later.allowed is True


def test_the_cooling_off_period_can_be_disabled():
    ledger = ContactLedger()
    ledger.record(contact(at=TS, key="a"))
    ruling = may_contact("cust-1", TS + timedelta(hours=1), consent=allowing(),
                         contacts=ledger, policy=ConductPolicy(min_hours_between=0))
    assert ruling.allowed is True


def test_the_window_cap_binds_across_runs():
    """The whole point of the module. Three contacts spread over three separate
    runs exhaust the allowance, and the fourth is refused."""
    ledger = ContactLedger()
    policy = ConductPolicy(max_contacts_per_window=3, window_days=30,
                           min_hours_between=24)
    for day in (0, 2, 4):
        at = TS + timedelta(days=day)
        assert may_contact("cust-1", at, consent=allowing(), contacts=ledger,
                           policy=policy).allowed is True
        ledger.record(contact(at=at, key=f"run-{day}"))

    ruling = may_contact("cust-1", TS + timedelta(days=6), consent=allowing(),
                         contacts=ledger, policy=policy)
    assert ruling.allowed is False and ruling.rule == RULE_WINDOW_CAP
    assert "cap is 3" in ruling.reason


def test_the_cap_window_rolls_forward():
    ledger = ContactLedger()
    policy = ConductPolicy(max_contacts_per_window=1, window_days=30,
                           min_hours_between=0)
    ledger.record(contact(at=TS, key="a"))
    assert may_contact("cust-1", TS + timedelta(days=10), consent=allowing(),
                       contacts=ledger, policy=policy).allowed is False
    assert may_contact("cust-1", TS + timedelta(days=31), consent=allowing(),
                       contacts=ledger, policy=policy).allowed is True


def test_with_consent_and_no_contact_history_the_answer_is_yes():
    ruling = may_contact("cust-1", TS, consent=allowing(), contacts=ContactLedger())
    assert ruling.allowed is True and ruling.rule == RULE_ALLOWED
    assert ruling.summary() == {"allowed": True, "rule": RULE_ALLOWED,
                                "reason": "no stopping rule applies"}
    assert isinstance(ruling, Ruling)


# --------------------------------------------------------------------------
# ConductGate — what the engine calls
# --------------------------------------------------------------------------
def test_the_gate_maps_a_leak_to_a_customer():
    gate = ConductGate(consent=allowing("cust-9"), customer_for=lambda l: "cust-9")
    assert gate.customer_id(leak()) == "cust-9"
    assert gate(leak(), TS).allowed is True


def test_the_leaks_own_customer_ref_is_used_by_default():
    """Reconciliation carries the settlement's counterparty onto the leak, so
    caps are per-customer with no wiring at all."""
    gate = ConductGate(consent=allowing("cust-1"))
    l = leak(customer_ref="cust-1")
    assert gate.customer_id(l) == "cust-1"
    assert gate.is_per_customer(l) is True
    assert gate(l, TS).allowed is True
    # a second leak for the same customer shares the allowance
    assert gate.customer_id(leak("leak:2", customer_ref="cust-1")) == "cust-1"


def test_a_leak_with_no_counterparty_degrades_to_a_per_leak_cap_and_says_so():
    """Weaker scope, reported rather than passing for the real thing."""
    gate = ConductGate(consent=allowing("leak:1"))
    assert gate.customer_id(leak()) == "leak:1"
    assert gate.is_per_customer(leak()) is False
    assert gate(leak(), TS).allowed is True
    assert gate(leak("leak:2"), TS).allowed is False       # a different "customer"


def test_an_explicit_mapping_overrides_the_leaks_own_ref():
    gate = ConductGate(consent=allowing("override"),
                       customer_for=lambda l: "override")
    l = leak(customer_ref="cust-1")
    assert gate.customer_id(l) == "override"
    assert gate.is_per_customer(l) is True
    assert gate.is_per_customer(leak()) is True            # the mapping vouches for it


def test_the_gate_authorises_but_does_not_record():
    """Something that both authorises and records would make a dry run
    indistinguishable from a real one."""
    ledger = ContactLedger()
    gate = ConductGate(consent=allowing(), contacts=ledger,
                       customer_for=lambda l: "cust-1")
    gate(leak(), TS)
    gate(leak(), TS)
    assert ledger.size == 0                                # nothing consumed

    assert gate.note(leak(), Channel.UPI_RETRY, TS, "leak:1:attempt:0") is True
    assert gate.note(leak(), Channel.UPI_RETRY, TS, "leak:1:attempt:0") is False
    assert ledger.size == 1


def test_noting_a_contact_without_a_ledger_is_a_no_op():
    gate = ConductGate(consent=allowing(), customer_for=lambda l: "cust-1")
    assert gate.note(leak(), Channel.UPI_RETRY, TS, "k") is False
