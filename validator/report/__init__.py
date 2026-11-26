"""Запись отчёта в файлы."""
from __future__ import annotations

from pathlib import Path

from .brief import build_brief
from .build import build_report
from .interactive import to_interactive_html


def blocks_for(result, full: bool = False) -> list:
    return build_report(result) if full else build_brief(result)
from .render import to_html, to_markdown, to_pdf


def write_reports(result, out_path: str | Path, pdf: bool = True, html: bool = True, full: bool = False) -> dict[str, Path]:
    """out_path: report.md | report.pdf | report.html — остальные форматы кладутся рядом с тем же именем."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    base = out.with_suffix("")
    blocks = blocks_for(result, full)
    title = f"Отчёт о проверке: {result.doc.title}"
    written: dict[str, Path] = {}
    md = base.with_suffix(".md")
    md.write_text(to_markdown(blocks), encoding="utf-8")
    written["md"] = md
    js = base.with_suffix(".json")
    js.write_text(to_json(result), encoding="utf-8")
    written["json"] = js
    if html or out.suffix.lower() in (".html", ".htm"):
        h = base.with_suffix(".html")
        h.write_text(to_html(blocks, title), encoding="utf-8")
        written["html"] = h
        ih = base.parent / f"{base.name}_interactive.html"
        ih.write_text(to_interactive_html(result), encoding="utf-8")
        written["interactive"] = ih
    if pdf or out.suffix.lower() == ".pdf":
        p = base.with_suffix(".pdf")
        to_pdf(blocks, p, title)
        written["pdf"] = p
    return written


def to_json(result) -> str:
    """Машиночитаемый результат: требования, находки, итог — для CI и оценки качества (eval/)."""
    import json

    sim = dict(result.simulation)
    if sim.get("output") is not None:
        sim["output"] = sim["output"].model_dump()
    return json.dumps({
        "passport": result.passport,
        "verdict": result.verdict.model_dump(),
        "grounding": result.grounding,
        "requirements": [r.model_dump() for r in result.requirements],
        "assessments": {k: v.model_dump() for k, v in result.assessments.items()},
        "findings": [f.model_dump() for f in result.findings],
        "coverage": [c.model_dump() for c in result.coverage],
        "glossary_gaps": result.glossary_gaps,
        "simulation": sim,
        "profile": result.profile.model_dump() if result.profile else None,
        "consistency": result.consistency.model_dump() if result.consistency else None,
    }, ensure_ascii=False, indent=1)


def render_all(result, full: bool = False) -> dict[str, bytes | str]:
    """Для веба: все форматы в памяти."""
    import tempfile

    blocks = blocks_for(result, full)
    title = f"Отчёт о проверке: {result.doc.title}"
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "report.pdf"
        to_pdf(blocks, p, title)
        pdf_bytes = p.read_bytes()
    return {"md": to_markdown(blocks), "html": to_html(blocks, title), "pdf": pdf_bytes,
            "interactive": to_interactive_html(result)}
