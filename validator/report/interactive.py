"""Интерактивный отчёт «документ и находки»: самодостаточная HTML-страница (web/viewer.html + данные прогона).

Используется и на сайте (вкладка), и в CLI (файл report_interactive.html рядом с отчётом; открывается без интернета).
"""
from __future__ import annotations

import json

from ..config import ROOT
from ..criteria import ALL
from ..pipeline import RunResult

TEMPLATE = ROOT / "web" / "viewer.html"
PLACEHOLDER = "/*__CLARIFAI_DATA__*/null"


def viewer_data(r: RunResult) -> dict:
    return {
        "title": r.doc.title,
        "verdict": {"ready": r.verdict.ready, "summary": r.verdict.summary},
        "requirements": len(r.requirements),
        "lines": [{"n": l.no, "t": l.text} for l in r.doc.lines],
        "reqs": {x.id: {"doc_id": x.doc_id, "quote": x.quote, "l1": x.line_start, "summary": x.summary} for x in r.requirements},
        "findings": [{
            "id": f.id, "sev": f.severity, "crit": f.criterion,
            "critName": ALL[f.criterion].name if f.criterion in ALL else f.criterion,
            "sim": f.source == "simulation", "reqs": f.req_ids,
            "quotes": [{"text": q.text, "l1": q.line_start, "l2": q.line_end, "loc": q.location} for q in f.quotes],
            "problem": f.problem, "recommendation": f.recommendation, "rewrite": f.rewrite, "question": f.question,
            "evidence": f.evidence,
        } for f in r.active],
    }


def to_interactive_html(r: RunResult) -> str:
    data = json.dumps(viewer_data(r), ensure_ascii=False)
    data = data.replace("</", "<\\/")  # не даём строкам данных закрыть тег <script>
    return TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, data, 1)
