"""Рендеры отчёта: Markdown, HTML (самодостаточный), PDF (fpdf2, кириллический шрифт из assets/)."""
from __future__ import annotations

import html
import re
from pathlib import Path

from ..config import ROOT
from ..criteria import SEVERITY
from .blocks import H, P, Q, Bullets, Callout, Code, FindingCard, Table

FONTS = ROOT / "assets" / "fonts"
SEV_ICON = {"critical": "🔴", "major": "🟠", "minor": "🟡"}


# ---------------- Markdown ----------------
def _md_cell(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", "<br>")


def to_markdown(blocks: list) -> str:
    out: list[str] = []
    for b in blocks:
        if isinstance(b, H):
            out.append("#" * b.level + " " + b.text)
        elif isinstance(b, P):
            out.append(b.text)
        elif isinstance(b, Q):
            out.append("> " + b.text.replace("\n", "\n> "))
        elif isinstance(b, Callout):
            icon = {"ok": "✅", "warn": "⚠️", "bad": "⛔", "info": "ℹ️"}.get(b.kind, "")
            out.append(f"> {icon} **{b.title}**\n>\n> {b.text}")
        elif isinstance(b, Table):
            out.append("| " + " | ".join(_md_cell(h) for h in b.headers) + " |")
            out.append("|" + "---|" * len(b.headers))
            for r in b.rows:
                out.append("| " + " | ".join(_md_cell(c) for c in r) + " |")
        elif isinstance(b, Bullets):
            out.append("\n".join((f"{i}. " if b.ordered else "- ") + it for i, it in enumerate(b.items, 1)))
        elif isinstance(b, Code):
            out.append(f"```{b.lang}\n{b.text.rstrip()}\n```")
        elif isinstance(b, FindingCard):
            lines = [f"**{SEV_ICON[b.severity]} {b.id} · {SEVERITY[b.severity]} · {b.title}**", ""]
            for qt, loc in b.quotes:
                lines.append(f"> «{qt}» — *{loc}*")
            lines.append("")
            for k, v in b.fields:
                lines.append(f"- **{k}:** {v}")
            out.append("\n".join(lines))
        out.append("")
    return "\n".join(out).strip() + "\n"


# ---------------- HTML ----------------
def _inline(s: str) -> str:
    s = html.escape(s or "")
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s).replace("\n", "<br>")


CSS = """
:root{--fg:#1d2330;--muted:#5b6475;--line:#e3e6ec;--bg:#fff;--soft:#f6f7f9;--accent:#7c3aed;
--crit:#c62828;--major:#ef6c00;--minor:#b28704;--ok:#2e7d32}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 "Segoe UI",system-ui,-apple-system,Roboto,"DejaVu Sans",sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;margin:0 0 16px}h2{font-size:20px;margin:36px 0 12px;padding-top:12px;border-top:1px solid var(--line)}
h3{font-size:16px;margin:24px 0 8px}
table{border-collapse:collapse;width:100%;margin:8px 0 16px;font-size:13px}
th,td{border:1px solid var(--line);padding:6px 8px;vertical-align:top;text-align:left}
th{background:var(--soft);font-weight:600}table.small{font-size:12px}
td.c{text-align:center}td.fail{color:var(--crit);font-weight:700}td.okm{color:var(--ok)}
blockquote{margin:8px 0;padding:8px 12px;border-left:3px solid var(--accent);background:var(--soft);color:#333}
.callout{padding:14px 16px;border-radius:8px;margin:12px 0;border:1px solid}
.callout.ok{background:#edf7ee;border-color:#a5d6a7}.callout.warn{background:#fff6e5;border-color:#ffcc80}
.callout.bad{background:#fdecec;border-color:#ef9a9a}.callout.info{background:#f3efff;border-color:#c4b5fd}
.callout b.t{display:block;margin-bottom:4px;font-size:16px}
.card{border:1px solid var(--line);border-left:4px solid var(--minor);border-radius:6px;padding:10px 14px;margin:10px 0;background:#fff}
.card.critical{border-left-color:var(--crit)}.card.major{border-left-color:var(--major)}
.card .hd{font-weight:600;margin-bottom:6px}.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;color:#fff;margin-right:6px}
.badge.critical{background:var(--crit)}.badge.major{background:var(--major)}.badge.minor{background:var(--minor)}
.card dl{margin:6px 0 0;display:grid;grid-template-columns:200px 1fr;gap:4px 12px}.card dt{color:var(--muted)}.card dd{margin:0}
.loc{color:var(--muted);font-size:12px}
pre{background:#0f1420;color:#e6e6e6;padding:12px;border-radius:6px;overflow:auto;font-size:12px}
@media (max-width:700px){.card dl{grid-template-columns:1fr}main{padding:16px}}
@media print{h2{break-before:auto}.card,table{break-inside:avoid}}
"""


def to_html(blocks: list, title: str = "Отчёт о проверке требований") -> str:
    out = []
    for b in blocks:
        if isinstance(b, H):
            out.append(f"<h{b.level}>{_inline(b.text)}</h{b.level}>")
        elif isinstance(b, P):
            out.append(f"<p>{_inline(b.text)}</p>")
        elif isinstance(b, Q):
            out.append(f"<blockquote>{_inline(b.text)}</blockquote>")
        elif isinstance(b, Callout):
            out.append(f'<div class="callout {b.kind}"><b class="t">{_inline(b.title)}</b>{_inline(b.text)}</div>')
        elif isinstance(b, Table):
            th = "".join(f"<th>{_inline(h)}</th>" for h in b.headers)
            trs = []
            for r in b.rows:
                tds = []
                for c in r:
                    cls = ' class="c fail"' if c == "✗" else (' class="c okm"' if c == "✓" else (' class="c"' if c in ("—",) else ""))
                    tds.append(f"<td{cls}>{_inline(c)}</td>")
                trs.append("<tr>" + "".join(tds) + "</tr>")
            out.append(f'<table class="{"small" if b.small else ""}"><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table>')
        elif isinstance(b, Bullets):
            tag = "ol" if b.ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(i)}</li>" for i in b.items) + f"</{tag}>")
        elif isinstance(b, Code):
            out.append(f"<pre><code>{html.escape(b.text)}</code></pre>")
        elif isinstance(b, FindingCard):
            qs = "".join(f'<blockquote>«{_inline(q)}»<div class="loc">{_inline(l)}</div></blockquote>' for q, l in b.quotes)
            dl = "".join(f"<dt>{_inline(k)}</dt><dd>{_inline(v)}</dd>" for k, v in b.fields)
            out.append(f'<div class="card {b.severity}" id="{b.id}"><div class="hd"><span class="badge {b.severity}">{SEVERITY[b.severity]}</span>'
                       f'{b.id} · {_inline(b.title)}</div>{qs}<dl>{dl}</dl></div>')
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{''.join(out)}</main></body></html>")


# ---------------- PDF ----------------
def _pdf_text(s: str) -> str:
    # fpdf2 markdown: **bold**, __italic__, --underline-- → экранируем лишнее
    s = (s or "").replace("--", "‐‐").replace("__", "_\u200b_")
    return s.replace("🔴", "").replace("🟠", "").replace("🟡", "").replace("⚠", "(!)")


def to_pdf(blocks: list, path: str | Path, title: str = "Отчёт") -> None:
    import logging

    from fpdf import FPDF
    from fpdf.fonts import FontFace

    logging.getLogger("fontTools").setLevel(logging.ERROR)

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(14, 14, 14)
    pdf.set_auto_page_break(True, 14)
    pdf.add_font("DejaVu", "", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.add_font("DejaVu", "I", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "BI", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.add_font("Mono", "", str(FONTS / "DejaVuSansMono.ttf"))
    pdf.set_title(title)
    pdf.set_creator("AI Requirements Validator")
    pdf.alias_nb_pages()

    def footer():
        pdf.set_y(-10)
        pdf.set_font("DejaVu", "", 7)
        pdf.set_text_color(130)
        pdf.cell(0, 5, f"{title} — стр. {pdf.page_no()}/{{nb}}", align="C")
        pdf.set_text_color(0)

    pdf.footer = footer  # type: ignore[assignment]
    pdf.add_page()
    W = pdf.epw
    colors = {"critical": (198, 40, 40), "major": (239, 108, 0), "minor": (178, 135, 4),
              "ok": (46, 125, 50), "warn": (239, 108, 0), "bad": (198, 40, 40), "info": (124, 58, 237)}

    def para(text, size=9.5, style="", indent=0.0, color=(0, 0, 0)):
        pdf.set_font("DejaVu", style, size)
        pdf.set_text_color(*color)
        pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(W - indent, size * 0.5, _pdf_text(text), markdown=True, align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0)

    for b in blocks:
        if isinstance(b, H):
            size = {1: 16, 2: 13, 3: 11}.get(b.level, 10)
            if b.level == 2 and pdf.get_y() > pdf.h - 50:
                pdf.add_page()
            pdf.ln(3 if b.level > 1 else 0)
            para(b.text, size, "B")
            if b.level <= 2:
                pdf.set_draw_color(220)
                pdf.line(pdf.l_margin, pdf.get_y() + 0.5, pdf.l_margin + W, pdf.get_y() + 0.5)
            pdf.ln(2)
        elif isinstance(b, P):
            para(b.text)
            pdf.ln(1.5)
        elif isinstance(b, Q):
            y0 = pdf.get_y()
            para(b.text, 9, "", indent=4, color=(60, 60, 60))
            pdf.set_draw_color(124, 58, 237)
            pdf.set_line_width(0.6)
            pdf.line(pdf.l_margin + 1, y0, pdf.l_margin + 1, pdf.get_y())
            pdf.set_line_width(0.2)
            pdf.ln(1.5)
        elif isinstance(b, Callout):
            c = colors.get(b.kind, (124, 58, 237))
            y0 = pdf.get_y()
            para(b.title, 11, "B", indent=4, color=c)
            para(b.text, 9.5, "", indent=4)
            pdf.set_draw_color(*c)
            pdf.set_line_width(1.2)
            pdf.line(pdf.l_margin + 1, y0, pdf.l_margin + 1, pdf.get_y())
            pdf.set_line_width(0.2)
            pdf.ln(3)
        elif isinstance(b, Table):
            size = 7 if b.small else 8.5
            pdf.set_font("DejaVu", "", size)
            widths = b.widths or [1 / len(b.headers)] * len(b.headers)
            cw = tuple(w * W for w in widths)
            pdf.set_draw_color(200)
            with pdf.table(col_widths=cw, width=W, text_align="LEFT", line_height=size * 0.48,
                           headings_style=FontFace(emphasis="BOLD", fill_color=(240, 242, 246)),
                           markdown=True, padding=1) as t:
                hr = t.row()
                for h in b.headers:
                    hr.cell(_pdf_text(h))
                for r in b.rows:
                    row = t.row()
                    for c in r:
                        if c == "✗":
                            row.cell(c, style=FontFace(color=(198, 40, 40), emphasis="BOLD"))
                        elif c == "✓":
                            row.cell(c, style=FontFace(color=(46, 125, 50)))
                        else:
                            row.cell(_pdf_text(c))
            pdf.ln(2)
        elif isinstance(b, Bullets):
            for i, it in enumerate(b.items, 1):
                para((f"{i}. " if b.ordered else "• ") + it, 9.5, indent=3)
            pdf.ln(1.5)
        elif isinstance(b, Code):
            pdf.set_font("Mono", "", 6.5)
            pdf.set_fill_color(246, 247, 249)
            pdf.multi_cell(W, 3.2, b.text.replace("\t", "    "), fill=True, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        elif isinstance(b, FindingCard):
            if pdf.get_y() > pdf.h - 40:
                pdf.add_page()
            c = colors[b.severity]
            y0 = pdf.get_y()
            para(f"**{b.id} · {SEVERITY[b.severity]}** · {b.title}", 9.5, "", indent=4, color=c)
            for q, loc in b.quotes:
                para(f"«{q}»", 8.5, "", indent=6, color=(50, 50, 50))
                if loc:
                    para(loc, 7, "", indent=6, color=(120, 120, 120))
            for k, v in b.fields:
                para(f"**{k}:** {v}", 8.5, "", indent=4)
            pdf.set_draw_color(*c)
            pdf.set_line_width(1)
            if pdf.get_y() > y0:
                pdf.line(pdf.l_margin + 1, y0, pdf.l_margin + 1, pdf.get_y())
            pdf.set_line_width(0.2)
            pdf.ln(3)
    pdf.output(str(path))
