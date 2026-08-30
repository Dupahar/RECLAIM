"""Phase 26 tests — funded-moment prediction (architecture §7).

Two things must hold no matter what the model believes: it must never schedule a
debit inside the RBI notice window, and it must not pretend to see a pattern
that is not there. Most of these tests are one of those two.
"""
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from reclaim.timing import (
    Basis,
    FundedMoment,
    FundedMomentPredictor,
    Prediction,
    TimingError,
    fit,
    schedule_attempt,
)

D = Decimal


def moments(customer: str, *specs) -> list:
    """``specs`` of ``(day, hour)`` in August 2026, or ``(month, day, hour)``."""
    out = []
    for spec in specs:
        month, day, hour = (8, *spec) if len(spec) == 2 else spec
        out.append(FundedMoment(customer_id=customer,
                                at=datetime(2026, month, day, hour, 0, 0)))
    return out


def salary_history(customer="cust-1", *, n=12, day=2, hour=10) -> list:
    """A customer paid on the same day each month — the Indian salary cycle.

    A nominal day past a month's end lands on its last day, as a real salary
    credit does, so a `day=31` history is mostly-31 rather than impossible.
    """
    out = []
    for i in range(n):
        year, month = 2025 + (i // 12), (i % 12) + 1
        out.append(FundedMoment(
            customer_id=customer,
            at=datetime(year, month, min(day, monthrange(year, month)[1]), hour, 0, 0)))
    return out


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def test_funded_moment_validates_its_inputs():
    with pytest.raises(TimingError):
        FundedMoment(customer_id="", at=datetime(2026, 8, 2, 10))
    with pytest.raises(TimingError):
        FundedMoment(customer_id="c1", at="2026-08-02")


def test_fit_validates_min_support_and_rows():
    with pytest.raises(TimingError):
        fit([], min_support=0)
    with pytest.raises(TimingError):
        fit([], min_support=True)
    with pytest.raises(TimingError):
        fit(["not a moment"])


def test_predict_requires_a_customer_id():
    with pytest.raises(TimingError):
        fit(salary_history()).predict("")


def test_confidence_threshold_must_be_a_decimal():
    p = fit(salary_history()).predict("cust-1")
    with pytest.raises(TimingError):
        p.is_confident(0.4)


def test_next_after_requires_a_datetime():
    p = fit(salary_history()).predict("cust-1")
    with pytest.raises(TimingError):
        p.next_after("2026-08-26")


# --------------------------------------------------------------------------
# The signal
# --------------------------------------------------------------------------
def test_a_salary_cycle_is_found():
    p = fit(salary_history(day=2, hour=10)).predict("cust-1")
    assert p.basis is Basis.CUSTOMER and p.is_personal is True
    assert p.day_of_month == 2 and p.hour == 10
    assert p.day_confidence == D("1.0000") and p.hour_confidence == D("1.0000")
    assert p.support == 12
    assert "funded on day 2" in p.note


def test_the_mode_wins_not_the_average():
    """An average of the 1st and the 29th is the 15th, when the account is
    empty on the 15th."""
    history = (moments("c1", (1, 10), (1, 10), (1, 10), (1, 10))
               + moments("c1", (29, 10), (29, 10)))
    p = fit(history, min_support=6).predict("c1")
    assert p.day_of_month == 1
    assert p.day_confidence == D("0.6667")


def test_ties_break_to_the_lowest_value_for_determinism():
    history = moments("c1", (5, 9), (5, 9), (12, 14), (12, 14), (20, 3), (20, 3))
    a = fit(history, min_support=6).predict("c1")
    b = fit(list(reversed(history)), min_support=6).predict("c1")
    assert a.day_of_month == b.day_of_month == 5
    assert a.hour == b.hour == 3        # hours tie too; lowest wins


def test_a_diffuse_history_says_so_rather_than_guessing_confidently():
    history = moments("c1", (1, 8), (7, 9), (13, 10), (19, 11), (25, 12), (28, 13))
    p = fit(history, min_support=6).predict("c1")
    assert p.day_confidence == D("0.1667")
    assert p.is_confident() is False
    assert "history is diffuse" in p.note
    assert "prefer the fixed schedule" in p.note


# --------------------------------------------------------------------------
# The hierarchical fallback
# --------------------------------------------------------------------------
def test_a_thin_customer_falls_back_to_the_pooled_prior():
    history = salary_history("cust-1", n=12, day=2) + moments("cust-2", (2, 10))
    p = fit(history, min_support=6).predict("cust-2")
    assert p.basis is Basis.POOLED
    assert p.is_personal is False
    assert p.day_of_month == 2
    assert "not a personal prediction" in p.note
    assert "only 1 observation" in p.note


def test_an_unknown_customer_with_no_pool_gets_no_prediction():
    p = fit(moments("cust-1", (2, 10)), min_support=6).predict("cust-9")
    assert p.basis is Basis.NONE
    assert p.is_known is False and p.day_of_month is None
    assert p.is_confident() is False
    assert p.next_after(datetime(2026, 8, 26, 9)) is None


def test_the_predictor_reports_its_own_evidence():
    pred = fit(salary_history("cust-1", n=12) + moments("cust-2", (2, 10)),
               min_support=6)
    assert pred.min_support == 6
    assert pred.customer_count == 2
    assert pred.pooled_support == 13
    assert pred.support_for("cust-1") == 12
    assert pred.support_for("cust-2") == 1
    assert pred.support_for("nobody") == 0


# --------------------------------------------------------------------------
# next_after — the compliance floor
# --------------------------------------------------------------------------
def test_the_prediction_is_always_after_the_bound():
    p = fit(salary_history(day=2, hour=10)).predict("cust-1")
    bound = datetime(2026, 8, 26, 9, 0, 0)
    assert p.next_after(bound) == datetime(2026, 9, 2, 10, 0, 0)


def test_a_moment_later_today_is_taken_this_month():
    p = fit(salary_history(day=27, hour=14)).predict("cust-1")
    assert p.next_after(datetime(2026, 8, 27, 9)) == datetime(2026, 8, 27, 14)


def test_a_moment_already_past_today_rolls_to_next_month():
    p = fit(salary_history(day=27, hour=9)).predict("cust-1")
    assert p.next_after(datetime(2026, 8, 27, 14)) == datetime(2026, 9, 27, 9)


def test_a_day_that_does_not_exist_clamps_instead_of_skipping_a_cycle():
    """Predicting the 31st must not skip February entirely."""
    p = fit(salary_history(day=31, hour=10, n=12)).predict("cust-1")
    assert p.next_after(datetime(2027, 2, 1, 9)) == datetime(2027, 2, 28, 10)
    assert p.next_after(datetime(2026, 9, 1, 9)) == datetime(2026, 9, 30, 10)


def test_a_december_bound_rolls_into_the_next_year():
    p = fit(salary_history(day=2, hour=10)).predict("cust-1")
    assert p.next_after(datetime(2026, 12, 5, 9)) == datetime(2027, 1, 2, 10)


def test_the_bounds_timezone_is_preserved():
    p = fit(salary_history(day=2, hour=10)).predict("cust-1")
    bound = datetime(2026, 8, 26, 9, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    got = p.next_after(bound)
    assert got.tzinfo == bound.tzinfo
    assert got > bound


# --------------------------------------------------------------------------
# schedule_attempt — three separate failure modes, three responses
# --------------------------------------------------------------------------
NOTICE = datetime(2026, 8, 26, 9, 0, 0)
FALLBACK = datetime(2026, 8, 26, 9, 0, 0)


def test_a_confident_prediction_retimes_the_attempt():
    pred = fit(salary_history(day=2, hour=10))
    s = schedule_attempt(pred, "cust-1", notice_deadline=NOTICE, fallback=FALLBACK)
    assert s.retimed is True
    assert s.at == datetime(2026, 9, 2, 10)
    assert "re-timed" in s.reason and "customer history" in s.reason


def test_no_evidence_falls_back_to_the_fixed_schedule():
    pred = fit(moments("other", (2, 10)), min_support=6)
    s = schedule_attempt(pred, "cust-1", notice_deadline=NOTICE, fallback=FALLBACK)
    assert s.retimed is False and s.at == FALLBACK
    assert "no funded moments observed" in s.reason


def test_a_diffuse_prediction_falls_back_and_says_why():
    history = moments("c1", (1, 8), (7, 9), (13, 10), (19, 11), (25, 12), (28, 13))
    s = schedule_attempt(fit(history, min_support=6), "c1",
                         notice_deadline=NOTICE, fallback=FALLBACK)
    assert s.retimed is False and s.at == FALLBACK
    assert "below 0.40" in s.reason


def test_the_threshold_is_configurable():
    history = moments("c1", (1, 8), (1, 8), (7, 9), (13, 10), (19, 11), (25, 12))
    pred = fit(history, min_support=6)
    lenient = schedule_attempt(pred, "c1", notice_deadline=NOTICE, fallback=FALLBACK,
                               confidence_threshold=D("0.30"))
    assert lenient.retimed is True


def test_a_retimed_attempt_never_lands_inside_the_notice_window():
    """The property that makes this safe to ship: whatever the model says, the
    scheduled time is after the compliance floor."""
    pred = fit(salary_history(day=1, hour=6))
    for day in range(1, 29):
        notice = datetime(2026, 8, day, 12, 0, 0)
        s = schedule_attempt(pred, "cust-1", notice_deadline=notice, fallback=notice)
        assert s.at > notice


def test_a_fallback_before_the_notice_deadline_is_refused():
    """The timing model is not allowed to be handed an illegal baseline and
    quietly pass it through."""
    pred = fit(salary_history())
    with pytest.raises(TimingError) as exc:
        schedule_attempt(pred, "cust-1", notice_deadline=NOTICE,
                         fallback=NOTICE - timedelta(hours=1))
    assert "compliance window" in str(exc.value)


def test_schedule_validates_its_datetimes():
    pred = fit(salary_history())
    with pytest.raises(TimingError):
        schedule_attempt(pred, "cust-1", notice_deadline="2026-08-26", fallback=FALLBACK)
    with pytest.raises(TimingError):
        schedule_attempt(pred, "cust-1", notice_deadline=NOTICE, fallback="2026-08-26")


# --------------------------------------------------------------------------
# Shape and determinism
# --------------------------------------------------------------------------
def test_prediction_summary_is_serialisable():
    s = fit(salary_history(day=2, hour=10)).predict("cust-1").summary()
    assert s["day_of_month"] == 2 and s["hour"] == 10
    assert s["day_confidence"] == "1.0000" and s["basis"] == "customer history"
    assert Prediction(None, None, None, None, 0, Basis.NONE, "x").summary()[
        "day_confidence"] is None


def test_fitting_is_order_independent():
    history = salary_history(n=12)
    a = fit(history).predict("cust-1").summary()
    b = fit(list(reversed(history))).predict("cust-1").summary()
    assert a == b


def test_a_predictor_can_be_built_directly_from_history():
    """A model shipped as a table rather than fitted in-process."""
    pred = FundedMomentPredictor({"c1": moments("c1", (3, 11), (3, 11))}, [], min_support=2)
    assert pred.predict("c1").day_of_month == 3
    assert pred.predict("c2").basis is Basis.NONE
