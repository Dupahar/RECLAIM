"""Smoke test for the runnable demo — proves the end-to-end run produces the
expected honest numbers deterministically."""
from reclaim.demo import run_demo


def test_demo_runs_and_reports_expected_numbers():
    rep = run_demo()
    s = rep.summary()
    # s1 (exact, payout 4882) + s3 (fuzzy auto, 1500) reconciled = 6382
    assert s["matched"] == "6382.00 INR"
    # s2 short by 200 -> recovered
    assert s["recovered"] == "200.00 INR"
    assert s["ai_confirmed"] == 0          # s3 matched by fuzzy AUTO, not the AI band
    assert s["auto_matched"] == 1
    # s4 (750) truly missing -> the one residual leak
    assert s["residual_leaks"] == 1
    assert rep.residual_leaks[0].source_refs[0] == "s4"
    assert rep.ledger.is_globally_balanced("INR")
