import markdown, pathlib, html, datetime

BASE = pathlib.Path(r"C:\Users\mahaj\Downloads\Razorpay\fintech-grid")

DOCS = [
    ("00-shared-value-chain.md",                 "Shared Value Chain & Market Sizing"),
    ("01-track-ai-growth-agentic-commerce.md",   "Track 01 — AI Growth & Agentic Commerce"),
    ("02-track-ai-risk-manager.md",              "Track 02 — AI Risk Manager"),
    ("03-track-ai-revenue-recovery.md",          "Track 03 — AI Revenue Recovery"),
    ("04-track-ai-finance-controller.md",        "Track 04 — AI Finance Controller"),
    ("05-track-open.md",                         "Track 05 — Open Track"),
]

md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"])

CSS = """
@page { size: A4; margin: 18mm 15mm; }
* { box-sizing: border-box; }
body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 10.5px; line-height: 1.5;
       color: #1a1a1a; margin: 0; }
h1 { font-size: 20px; color: #0b3b8f; border-bottom: 3px solid #0b3b8f; padding-bottom: 6px;
     margin: 0 0 12px; }
h2 { font-size: 15px; color: #0b3b8f; margin: 18px 0 6px; border-bottom: 1px solid #d0d7e2; padding-bottom: 3px; }
h3 { font-size: 12.5px; color: #12579d; margin: 12px 0 4px; }
p { margin: 6px 0; }
a { color: #1155cc; text-decoration: none; word-break: break-word; }
code { background: #f2f4f8; padding: 1px 4px; border-radius: 3px; font-family: 'Consolas', monospace; font-size: 9.5px; }
pre { background: #f6f8fa; border: 1px solid #dfe3e8; border-radius: 6px; padding: 10px;
      overflow-x: auto; font-size: 9px; line-height: 1.35; page-break-inside: avoid; }
pre code { background: none; padding: 0; }
blockquote { border-left: 4px solid #ffb020; background: #fff8ec; margin: 8px 0; padding: 6px 12px;
             color: #4a3b1a; border-radius: 0 4px 4px 0; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 9.3px; page-break-inside: avoid; }
th, td { border: 1px solid #cbd2dd; padding: 4px 6px; text-align: left; vertical-align: top; }
th { background: #eef2f8; color: #0b3b8f; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfd; }
ul, ol { margin: 6px 0; padding-left: 20px; }
li { margin: 2px 0; }
hr { border: none; border-top: 1px solid #d0d7e2; margin: 14px 0; }
.section { page-break-before: always; }
.cover { page-break-after: always; text-align: center; padding-top: 120px; }
.cover h1 { font-size: 34px; border: none; color: #0b3b8f; margin-bottom: 4px; }
.cover .sub { font-size: 15px; color: #444; margin-top: 4px; }
.cover .meta { margin-top: 40px; font-size: 11px; color: #666; }
.cover .rule { width: 90px; height: 4px; background: #ffb020; margin: 18px auto; border-radius: 2px; }
.toc { page-break-after: always; }
.toc h1 { font-size: 22px; }
.toc ol { font-size: 12px; line-height: 2; }
.disclaimer { background: #f2f4f8; border: 1px solid #dfe3e8; border-radius: 6px; padding: 10px 14px;
              font-size: 9.5px; color: #444; margin-top: 30px; text-align: left; }
"""

today = datetime.date(2026, 8, 25).strftime("%d %B %Y")

cover = f"""
<div class="cover">
  <h1>India Fintech</h1>
  <div class="sub">Value-Chain Research &amp; Opportunity Grids</div>
  <div class="rule"></div>
  <div class="sub">Value-Chain Research — Five Segments</div>
  <div class="meta">
    India primary &middot; global secondary<br/>
    Compiled {today}<br/>
    Synthesis, not prescription &middot; every claim carries a confidence level and a source
  </div>
  <div class="disclaimer">
    <b>Sourcing note.</b> Prepared under strict accuracy rules: factual claims are cited to real
    retrieved URLs; conflicting figures are flagged rather than resolved by guessing; market-size
    numbers show their calculations and assumptions; and confidence levels (High / Medium / Low)
    accompany non-trivial claims. This is a <b>light</b> research pass — investor/funding rows and
    some startup lists are explicitly marked &ldquo;not verified&rdquo; and are the first candidates
    for deeper research.
  </div>
</div>
"""

toc_items = "\n".join(
    f'<li>{html.escape(title)}</li>' for _, title in DOCS
)
toc = f'<div class="toc"><h1>Contents</h1><ol>{toc_items}</ol></div>'

sections = []
for i, (fname, title) in enumerate(DOCS):
    text = (BASE / fname).read_text(encoding="utf-8")
    md.reset()
    body = md.convert(text)
    cls = "section" if i > 0 else "section"
    sections.append(f'<div class="{cls}">{body}</div>')

doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{CSS}</style></head>
<body>{cover}{toc}{"".join(sections)}</body></html>"""

out_html = BASE / "_combined.html"
out_html.write_text(doc, encoding="utf-8")
print("HTML written:", out_html)
