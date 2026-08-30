"""Post-Sprint-3 tests — drift monitoring (architecture §9.2).

A monitor is only worth having if it fires when it should and stays quiet when
it should. Both halves are tested against constructed data where the answer is
known, plus the refusals that keep it credible on thin windows.
"""
from decimal import Decimal

import pytest

from reclaim.drift import (
    DriftError,
    DriftVerdict,
    detect_drift,
    scan,
    summarise,
    windows,
)
from reclaim.offline_eval import LoggedDecision

D = Decimal


def rows(n, *, successes, action="upi_retry|10|firm", start=0):
    """``n`` rows of which ``successes`` have reward 1."""
    return [LoggedDecision(unit_id=f"u{start + i}", context_key="ctx",
                           action_key=action, propensity=D("0.5"),
                           reward=D("1") if i < successes else D("0"))
            for i in range(n)]


def mixed(n, *, successes, split, action_a="upi_retry|10|firm",
          action_b="whatsapp_nudge|10|gentle"):
    """``n`` rows where the first ``split`` use ``action_a``."""
    out = rows(n, successes=successes)
    return [LoggedDecision(r.unit_id, r.context_key,
                           action_a if i < split else action_b,
                           r.propensity, r.reward)
            for i, r in enumerate(out)]


# --------------------------------------------------------------------------
# WindowStats
# --------------------------------------------------------------------------
def test_summarise_reports_mean_and_action_mix():
    stats = summarise(mixed(100, successes=40, split=75))
    assert stats.n == 100
    assert stats.mean == D("0.4000")
    assert stats.successes == 40
    assert stats.binary is True
    assert stats.action_mix() == {"upi_retry|10|firm": D("0.7500"),
                                  "whatsapp_nudge|10|gentle": D("0.2500")}
    assert stats.summary()["mean"] == "0.4000"


def test_an_empty_window_has_no_mean_and_no_mix():
    stats = summarise([])
    assert stats.n == 0 and stats.mean is None and stats.action_mix() == {}
    assert stats.summary()["mean"] is None


def test_non_binary_rewards_are_detected_not_silently_averaged():
    rewards = rows(10, successes=5)
    rewards[0] = LoggedDecision("u0", "ctx", "a|10|m", D("0.5"), D("250.00"))
    stats = summarise(rewards)
    assert stats.binary is False
    assert stats.successes is None
    assert stats.mean is not None            # a mean is still meaningful


def test_summarise_rejects_a_non_log_row():
    with pytest.raises(DriftError):
        summarise(["not a row"])


# --------------------------------------------------------------------------
# The verdicts
# --------------------------------------------------------------------------
def test_a_stable_policy_is_not_flagged():
    report = detect_drift(rows(200, successes=100),
                          rows(200, successes=104, start=200))
    assert report.verdict is DriftVerdict.STABLE
    assert report.is_actionable is False
    assert "consistent with noise" in report.note


def test_a_real_degradation_is_flagged():
    report = detect_drift(rows(200, successes=140),
                          rows(200, successes=80, start=200))
    assert report.verdict is DriftVerdict.DEGRADED
    assert report.is_actionable is True
    assert report.delta == D("-0.3000")
    assert report.z < D("-2")
    assert "re-evaluate the policy" in report.note


def test_an_improvement_is_reported_but_not_actionable():
    report = detect_drift(rows(200, successes=80),
                          rows(200, successes=140, start=200))
    assert report.verdict is DriftVerdict.IMPROVED
    assert report.is_actionable is False
    assert "reference window is now stale" in report.note


def test_a_small_real_change_stays_inside_the_noise_band():
    """The monitor must not fire on a 2pp move in 200 samples, or it gets muted."""
    report = detect_drift(rows(200, successes=100),
                          rows(200, successes=104, start=200))
    assert report.verdict is DriftVerdict.STABLE


def test_the_threshold_is_configurable():
    lenient = detect_drift(rows(200, successes=110), rows(200, successes=90, start=200),
                           z_threshold=D("1"))
    assert lenient.verdict is DriftVerdict.DEGRADED
    strict = detect_drift(rows(200, successes=110), rows(200, successes=90, start=200),
                          z_threshold=D("5"))
    assert strict.verdict is DriftVerdict.STABLE


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------
def test_a_thin_window_yields_no_verdict():
    """A monitor that cries drift on twelve observations gets muted, after which
    its silence is uninformative too."""
    report = detect_drift(rows(12, successes=10), rows(12, successes=2, start=12))
    assert report.verdict is DriftVerdict.INSUFFICIENT_DATA
    assert report.delta is None and report.z is None
    assert report.is_actionable is False
    assert "below min_n" in report.note


def test_min_n_is_configurable_for_a_high_volume_deployment():
    report = detect_drift(rows(40, successes=32), rows(40, successes=8, start=40),
                          min_n=40)
    assert report.verdict is DriftVerdict.DEGRADED


def test_non_binary_rewards_get_a_delta_but_no_significance_test():
    ref = [LoggedDecision(f"r{i}", "ctx", "a|10|m", D("0.5"), D("100")) for i in range(50)]
    cur = [LoggedDecision(f"c{i}", "ctx", "a|10|m", D("0.5"), D("40")) for i in range(50)]
    report = detect_drift(ref, cur)
    assert report.z is None
    assert report.delta == D("-60.0000")
    assert report.verdict is DriftVerdict.STABLE
    assert "does not apply" in report.note


def test_unanimous_windows_have_no_variance_to_test():
    report = detect_drift(rows(50, successes=0), rows(50, successes=0, start=50))
    assert report.z is None
    assert report.delta == D("0.0000")
    assert "no variance to test against" in report.note


def test_detect_drift_validates_its_arguments():
    ref, cur = rows(50, successes=25), rows(50, successes=25, start=50)
    for bad in (0, -1, True):
        with pytest.raises(DriftError):
            detect_drift(ref, cur, min_n=bad)
    for bad in (2, D("0"), D("-1")):
        with pytest.raises(DriftError):
            detect_drift(ref, cur, z_threshold=bad)


# --------------------------------------------------------------------------
# Action-mix drift — the earlier signal
# --------------------------------------------------------------------------
def test_a_collapsed_action_mix_is_flagged_even_when_reward_holds():
    """A policy that stops exploring changes its mix well before its reward
    becomes distinguishable. Stable reward plus a shifted mix is a different
    situation from a falling reward, so they are reported separately."""
    ref = mixed(200, successes=100, split=100)          # 50/50
    cur = mixed(200, successes=100, split=200)           # all one arm
    report = detect_drift(ref, cur)
    assert report.verdict is DriftVerdict.STABLE
    assert report.mix_shift == D("0.5000")
    assert report.mix_shifted is True
    assert "ACTION MIX SHIFTED" in report.note


def test_an_unchanged_mix_is_not_flagged():
    report = detect_drift(mixed(200, successes=100, split=100),
                          mixed(200, successes=100, split=100))
    assert report.mix_shift == D("0.0000")
    assert report.mix_shifted is False
    assert "ACTION MIX" not in report.note


def test_mix_shift_is_reported_even_on_a_thin_window():
    """It needs no significance test, so thin data does not suppress it."""
    report = detect_drift(mixed(10, successes=5, split=5),
                          mixed(10, successes=5, split=10))
    assert report.verdict is DriftVerdict.INSUFFICIENT_DATA
    assert report.mix_shift == D("0.5000")


def test_two_empty_windows_have_no_mix_distance():
    report = detect_drift([], [])
    assert report.mix_shift == D("0")
    assert report.verdict is DriftVerdict.INSUFFICIENT_DATA


# --------------------------------------------------------------------------
# Scanning a whole log
# --------------------------------------------------------------------------
def test_windows_drops_the_trailing_remainder():
    """A partial trailing window is the one most likely to produce a spurious
    verdict, so every compared window carries the same weight."""
    chunks = windows(rows(250, successes=125), 100)
    assert [len(c) for c in chunks] == [100, 100]


def test_windows_validates_its_size():
    for bad in (0, -1, True):
        with pytest.raises(DriftError):
            windows(rows(10, successes=5), bad)
    with pytest.raises(DriftError):
        windows(["not a row"], 1)


def test_scan_compares_every_window_to_the_first():
    """A slow decay would pass a previous-window comparison every time while the
    policy quietly halved. A fixed reference makes cumulative drift visible."""
    log = (rows(100, successes=70, start=0)
           + rows(100, successes=60, start=100)
           + rows(100, successes=35, start=200))
    reports = scan(log, size=100)
    assert len(reports) == 2
    assert reports[0].verdict is DriftVerdict.STABLE       # -10pp, inside noise
    assert reports[1].verdict is DriftVerdict.DEGRADED     # -35pp, clear
    assert [r.reference.n for r in reports] == [100, 100]


def test_scan_needs_at_least_two_windows():
    with pytest.raises(DriftError) as exc:
        scan(rows(100, successes=50), size=100)
    assert "at least two windows" in str(exc.value)


def test_a_report_summary_is_serialisable():
    s = detect_drift(rows(200, successes=140),
                     rows(200, successes=80, start=200)).summary()
    assert s["verdict"] == "degraded" and s["actionable"] is True
    assert s["reference"]["n"] == 200 and s["current"]["n"] == 200
    for key in ("delta", "z", "mix_shift", "mix_shifted", "note"):
        assert key in s
