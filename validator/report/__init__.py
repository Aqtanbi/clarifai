"""Запись отчёта в файлы."""
from __future__ import annotations

from pathlib import Path

from .brief import build_brief
from .build import build_report
from .interactive import to_interactive_html


def _safe_write(path: Path, writer) -> Path:
    """Пишет файл; если он занят (открыт в просмотрщике на Windows) — сохраняет рядом под другим именем."""
    try:
        writer(path)
        return path
    except PermissionError:
        from datetime import datetime

        alt = path.with_name(f"{path.stem}_{datetime.now():%H%M%S}{path.suffix}")
        writer(alt)
        print(f"Внимание: {path} открыт в другой программе — отчёт сохранён как {alt}")
        return alt


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
    md_text = to_markdown(blocks)
    written["md"] = _safe_write(base.with_suffix(".md"), lambda p: p.write_text(md_text, encoding="utf-8"))
    js_text = to_json(result)
    written["json"] = _safe_write(base.with_suffix(".json"), lambda p: p.write_text(js_text, encoding="utf-8"))
    if html or out.suffix.lower() in (".html", ".htm"):
        html_text = to_html(blocks, title)
        written["html"] = _safe_write(base.with_suffix(".html"), lambda p: p.write_text(html_text, encoding="utf-8"))
        int_text = to_interactive_html(result)
        written["interactive"] = _safe_write(base.parent / f"{base.name}_interactive.html",
                                             lambda p: p.write_text(int_text, encoding="utf-8"))
    if pdf or out.suffix.lower() == ".pdf":
        written["pdf"] = _safe_write(base.with_suffix(".pdf"), lambda p: to_pdf(blocks, p, title))
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
