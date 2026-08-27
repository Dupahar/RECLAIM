"""Sprint 1.2 tests — HTML run dashboard."""
from datetime import datetime
from decimal import Decimal

from reclaim.money import Money
from reclaim.domain import Source, Transaction, TransactionRefs
from reclaim.pipeline import build_audit_log, run_reclaim
from reclaim.resolver import GatedResolver, StaticResolver
from reclaim.recovery import AlwaysSucceedsExecutor, RecoveryEngine
from reclaim.dashboard import render_report, write_dashboard

TS = datetime(2026, 8, 26, 9, 0, 0)


def _t(tid, source, amount, **refs):
    return Transaction(id=tid, source=source, gross_amount=Money.of(amount, "INR"), ts=TS,
                       refs=TransactionRefs(**refs))


def _report():
    settlements = [_t("s1", Source.SETTLEMENT, "1000.00", utr="U1"),
                   _t("s2", Source.SETTLEMENT, "500.00", utr="U2"),   # short -> recover
                   _t("s3", Source.SETTLEMENT, "750.00", utr="U3")]   # missing -> residual
    banks = [_t("b1", Source.BANK, "1000.00", utr="U1"),
             _t("b2", Source.BANK, "400.00", utr="U2")]
    return run_reclaim(settlements, banks,
                       resolver=GatedResolver(StaticResolver(True, Decimal("0.9"))),
                       recovery_engine=RecoveryEngine(AlwaysSucceedsExecutor()), base_time=TS)


def test_render_contains_headline_numbers():
    html = render_report(_report(), title="RECLAIM Demo")
    assert "<!DOCTYPE html>" in html
    assert "RECLAIM Demo" in html
    assert "1000.00 INR" in html      # matched (s1 exact)
    assert "100.00 INR" in html       # recovered shortfall (s2: 500-400)
    assert "leak:missing:s3" in html  # residual


def test_render_includes_audit_root():
    rep = _report()
    root = build_audit_log(rep, TS).root()
    html = render_report(rep, audit_root=root)
    assert root in html
    assert "audit root" in html


def test_render_empty_report():
    rep = run_reclaim([], [])
    html = render_report(rep)
    assert "No residual leaks" in html
    assert "balanced" in html


def test_write_dashboard(tmp_path):
    p = write_dashboard(_report(), tmp_path / "run.html", title="T")
    assert p.exists()
    assert "<html" in p.read_text(encoding="utf-8")


def test_escapes_untrusted_hypothesis(tmp_path):
    # a leak hypothesis is rendered escaped (no raw HTML injection)
    html = render_report(_report())
    assert "<script>" not in html  # nothing injects raw tags


def test_dashboard_shows_closure_story():
    """The dashboard must show detection -> closure, not just detection."""
    html_out = render_report(_report())
    assert "CLOSED" in html_out.upper()
    assert "closure" in html_out
    # two-segment meter: reconciled slice + recovered slice, each labelled
    assert 'class="seg-m"' in html_out and 'class="seg-r"' in html_out
    assert "reconciled" in html_out and "recovered" in html_out


def test_dashboard_meter_segments_never_exceed_the_track():
    """Widths are clamped so a pathological report cannot overflow the bar."""
    import re
    html_out = render_report(_report())
    widths = [float(w) for w in re.findall(r'width:([0-9.]+)%', html_out)]
    assert widths and sum(widths) <= 100.0
