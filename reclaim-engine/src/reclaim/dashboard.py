"""Render a RunReport as a self-contained HTML dashboard (the demo artifact).

No external dependencies, no JS — a single static HTML string you can open in a
browser or hand to a judge. Shows the honest three numbers (match rate /
matched / recovered), the residual exception list, the AI + recovery audit, and
the tamper-evident audit root.
"""
from __future__ import annotations

import html
import pathlib

from .pipeline import RunReport

_CSS = """
* { box-sizing: border-box; }
body { font-family: 'Segoe UI', Arial, sans-serif; margin: 0; background: #eef2f8; color: #14203a; }
.wrap { max-width: 960px; margin: 0 auto; padding: 28px 20px 48px; }
h1 { color: #0b3b8f; margin: 0 0 2px; letter-spacing: 2px; }
.sub { color: #556; margin: 0 0 22px; }
.cards { display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 22px; }
.card { flex: 1 1 180px; background: #fff; border: 1px solid #d7deea; border-radius: 10px; padding: 16px 18px; }
.card .label { font-size: 12px; text-transform: uppercase; letter-spacing: .5px; color: #6a768c; }
.card .value { font-size: 26px; font-weight: 700; color: #0b3b8f; margin-top: 4px; }
.card.good .value { color: #1a7f4b; }
.card.warn .value { color: #b8531a; }
.bar { height: 12px; background: #dbe3f0; border-radius: 6px; overflow: hidden; margin: 6px 0 22px; }
.bar > span { display: block; height: 100%; background: linear-gradient(90deg,#0b3b8f,#2f6fd0); }
h2 { color: #0b3b8f; font-size: 16px; margin: 22px 0 8px; border-bottom: 1px solid #d7deea; padding-bottom: 4px; }
table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d7deea; border-radius: 8px; overflow: hidden; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef1f6; }
th { background: #f4f7fc; color: #0b3b8f; }
tr:last-child td { border-bottom: none; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.pill.ok { background: #e3f5ec; color: #1a7f4b; }
.pill.bad { background: #fdece4; color: #b8531a; }
.mono { font-family: Consolas, monospace; font-size: 12px; color: #445; word-break: break-all; }
.foot { margin-top: 26px; color: #6a768c; font-size: 12px; }
"""


def _card(label: str, value: str, cls: str = "") -> str:
    return (f'<div class="card {cls}"><div class="label">{html.escape(label)}</div>'
            f'<div class="value">{html.escape(value)}</div></div>')


def render_report(report: RunReport, *, title: str = "RECLAIM", audit_root: str | None = None) -> str:
    s = report.summary()
    rate_pct = float(report.match_rate()) * 100
    balanced = report.ledger.is_globally_balanced(report.currency)

    cards = "".join([
        _card("Total expected", s["total_expected"]),
        _card("Matched", s["matched"], "good"),
        _card("Recovered", s["recovered"], "good"),
        _card("Residual", s["residual"], "warn"),
    ])

    residual_rows = "".join(
        f"<tr><td>{html.escape(l.id)}</td><td>{html.escape(str(l.amount))}</td>"
        f"<td>{html.escape(l.leak_type.value)}</td><td>{html.escape(l.hypothesis)}</td></tr>"
        for l in report.residual_leaks
    ) or '<tr><td colspan="4">No residual leaks — everything reconciled or recovered.</td></tr>'

    balanced_pill = ('<span class="pill ok">balanced</span>' if balanced
                     else '<span class="pill bad">UNBALANCED</span>')
    root_html = (f'<p class="mono">audit root: {html.escape(audit_root)}</p>'
                 if audit_root else "")

    body = f"""
<div class="wrap">
  <h1>{html.escape(title)}</h1>
  <p class="sub">Reconciliation-Enabled Closed-Loop AI for Integrity &amp; Money-recovery — run report</p>
  <div class="cards">{cards}</div>
  <div class="label" style="color:#6a768c;font-size:12px;">match rate {s['match_rate']} ({rate_pct:.1f}% of expected)</div>
  <div class="bar"><span style="width:{min(rate_pct,100):.1f}%"></span></div>

  <h2>What happened</h2>
  <table>
    <tr><th>auto-matched (fuzzy)</th><th>AI confirmed</th><th>AI escalated</th><th>pending review</th><th>recovered</th></tr>
    <tr><td>{s['auto_matched']}</td><td>{s['ai_confirmed']}</td><td>{s['ai_escalated']}</td><td>{s['pending_review']}</td><td>{s['recovered_count']}</td></tr>
  </table>

  <h2>Honest residual exception list ({s['residual_leaks']})</h2>
  <table>
    <tr><th>leak id</th><th>amount</th><th>type</th><th>hypothesis</th></tr>
    {residual_rows}
  </table>

  <h2>Integrity</h2>
  <p>double-entry ledger ({html.escape(report.currency)}): {balanced_pill}</p>
  {root_html}

  <p class="foot">Deterministic engine. AI resolver and payment executor plug in behind tested interfaces.</p>
</div>
"""
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(title)} — run report</title><style>{_CSS}</style></head>"
            f"<body>{body}</body></html>")


def write_dashboard(report: RunReport, path, *, title: str = "RECLAIM",
                    audit_root: str | None = None) -> pathlib.Path:
    p = pathlib.Path(path)
    p.write_text(render_report(report, title=title, audit_root=audit_root), encoding="utf-8")
    return p
