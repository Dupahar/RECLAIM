import markdown, pathlib, re, datetime

BASE = pathlib.Path(r"C:\Users\mahaj\Downloads\Razorpay")
SRC = BASE / "RECLAIM-System-Architecture.md"
text = SRC.read_text(encoding="utf-8")

mermaid_blocks = []
def _stash(m):
    mermaid_blocks.append(m.group(1)); return f"\n\nMERMAIDPH{len(mermaid_blocks)-1}END\n\n"
text = re.sub(r"```mermaid\n(.*?)```", _stash, text, flags=re.DOTALL)

md = markdown.Markdown(extensions=["tables","fenced_code","toc","sane_lists","attr_list"])
body = md.convert(text)
body = re.sub(r"(?:<p>)?MERMAIDPH(\d+)END(?:</p>)?", lambda m: f'<div class="mermaid">{mermaid_blocks[int(m.group(1))]}</div>', body)

CSS = """
@page { size: A4; margin: 15mm 13mm; }
* { box-sizing: border-box; }
body { font-family:'Segoe UI',Arial,sans-serif; font-size:10.3px; line-height:1.55; color:#14203a; margin:0; }
h1 { font-size:21px; color:#0b3b8f; margin:4px 0 8px; }
h2 { font-size:15px; color:#0b3b8f; margin:18px 0 6px; border-bottom:2px solid #dbe3f0; padding-bottom:4px; page-break-after:avoid; }
h3 { font-size:12px; color:#12579d; margin:11px 0 4px; page-break-after:avoid; }
p { margin:6px 0; } a { color:#1155cc; text-decoration:none; word-break:break-word; }
strong { color:#0b2a5e; }
code { background:#eef1f6; padding:1px 4px; border-radius:3px; font-family:Consolas,monospace; font-size:9px; }
pre { background:#f6f8fa; border:1px solid #dfe3e8; border-radius:6px; padding:9px; overflow-x:auto; font-size:8.6px; line-height:1.4; page-break-inside:avoid; }
pre code { background:none; padding:0; }
blockquote { border-left:4px solid #ffb020; background:#fff8ec; margin:8px 0; padding:6px 12px; color:#4a3b1a; border-radius:0 4px 4px 0; }
table { border-collapse:collapse; width:100%; margin:8px 0; font-size:8.8px; page-break-inside:avoid; }
th,td { border:1px solid #cbd2dd; padding:4px 6px; text-align:left; vertical-align:top; }
th { background:#eef2f8; color:#0b3b8f; font-weight:600; } tr:nth-child(even) td { background:#fafbfd; }
ul,ol { margin:6px 0; padding-left:20px; } li { margin:2px 0; }
hr { border:none; border-top:1px solid #d0d7e2; margin:14px 0; }
.mermaid { text-align:center; margin:14px 0; page-break-inside:avoid; }
.cover { text-align:center; padding-top:140px; page-break-after:always; }
.cover .brand { font-size:50px; font-weight:800; letter-spacing:4px; color:#0b3b8f; margin:0; }
.cover .sub { font-size:14px; color:#12579d; font-style:italic; margin:8px 0 0; }
.cover .rule { width:110px; height:5px; background:#ffb020; margin:20px auto; border-radius:3px; }
.cover .tag { font-size:15px; color:#333; font-weight:600; }
.cover .meta { margin-top:40px; font-size:11px; color:#666; line-height:1.8; }
"""

MERMAID = """
<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
mermaid.initialize({ startOnLoad:true, theme:'neutral', flowchart:{htmlLabels:true, curve:'basis'}, securityLevel:'loose' });
</script>
"""
today = datetime.date(2026,8,25).strftime("%d %B %Y")
cover = f"""
<div class="cover">
  <p class="brand">RECLAIM</p>
  <p class="sub">System Design &amp; Architecture</p>
  <div class="rule"></div>
  <p class="tag">A verification system that happens to use AI &mdash;<br/>correct with money, honest about its limits, safe to run autonomously.</p>
  <div class="meta">Architecture &middot; v1.0<br/>Companion to the RECLAIM Product Document<br/>Grounded in cited papers, OSS &amp; production write-ups<br/>Compiled {today}</div>
</div>
"""
doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{CSS}</style>{MERMAID}</head><body>{cover}{body}</body></html>"""
out = BASE / "fintech-grid" / "_arch_combined.html"
out.write_text(doc, encoding="utf-8")
print("HTML written:", out)
