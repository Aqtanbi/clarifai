"""MCP-сервер агента: инструменты для Claude Desktop / Cursor / любого MCP-клиента.

Локально — stdio (`python mcp_server.py`), на сайте — Streamable HTTP по адресу /mcp.
"""
from __future__ import annotations

import json
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .criteria import INDIVIDUAL, SET
from .grounding import Grounder
from .llm import LLM
from .parsing import load_document
from .pipeline import Options, Pipeline
from .report import blocks_for, write_reports
from .report.render import to_markdown

INSTRUCTIONS = (
    "ClarifAI — агент статической проверки требований по ISO/IEC/IEEE 29148:2018 и IEEE 830-1998. "
    "validate_requirements — полный прогон (нарезка, проверка требований и набора, проверка расчётом, рецензент) "
    "и отчёт на русском; extract_requirements — только нарезка; verify_quote — проверить, что цитата есть в документе; "
    "list_criteria — критерии проверки. Полный прогон занимает 1–4 минуты."
)


def _summary(res) -> dict:
    act = res.active
    return {
        "document": res.doc.title,
        "requirements": len(res.requirements),
        "violations": len(act),
        "critical": sum(f.severity == "critical" for f in act),
        "ready_for_development": res.verdict.ready,
        "verdict": res.verdict.summary,
        "top_fixes": [t.action for t in res.verdict.top_fixes],
    }


def build_server(remote: bool = False) -> MCPServer:
    mcp = MCPServer("clarifai", instructions=INSTRUCTIONS, version="0.1.0")

    @mcp.tool(description="Полная проверка документа требований. Возвращает сводку и отчёт в Markdown. "
                          "text — текст документа (markdown/plain), filename — имя для паспорта отчёта.")
    def validate_requirements(text: str, filename: str = "requirements.md", simulate: bool = True, full: bool = False) -> str:
        doc = load_document(data=text.encode("utf-8"), filename=filename if Path(filename).suffix in (".md", ".txt") else filename + ".md")
        res = Pipeline(llm=LLM(), options=Options(simulate=simulate)).run(doc)
        return json.dumps(_summary(res), ensure_ascii=False, indent=1) + "\n\n" + to_markdown(blocks_for(res, full))

    @mcp.tool(description="Выделить отдельные требования из документа (без оценки). Возвращает JSON-список.")
    def extract_requirements(text: str, filename: str = "requirements.md") -> str:
        doc = load_document(data=text.encode("utf-8"), filename=filename if Path(filename).suffix in (".md", ".txt") else filename + ".md")
        p = Pipeline(llm=LLM())
        reqs = p.extract(doc, Grounder(doc))
        return json.dumps([r.model_dump(exclude_none=True) for r in reqs], ensure_ascii=False, indent=1)

    @mcp.tool(description="Проверить, что цитата дословно (или почти дословно) присутствует в документе; вернуть место. Без LLM.")
    def verify_quote(text: str, quote: str) -> str:
        doc = load_document(data=text.encode("utf-8"), filename="doc.md")
        return json.dumps(Grounder(doc).locate(quote), ensure_ascii=False)

    @mcp.tool(description="Критерии качества требований (ISO 29148 п. 5.2.5/5.2.6, IEEE 830 п. 4.3), по которым работает агент.")
    def list_criteria() -> str:
        return json.dumps([{"code": c.code, "name": c.name, "source": c.source, "question": c.question} for c in INDIVIDUAL + SET],
                          ensure_ascii=False, indent=1)

    if not remote:
        @mcp.tool(description="Проверить файл требований на диске (PDF/DOCX/MD/TXT) и записать отчёт (MD, HTML, PDF). "
                              "Возвращает сводку и пути к файлам отчёта.")
        def validate_requirements_file(path: str, output: str = "reports/report.md", simulate: bool = True, pdf: bool = True,
                                       full: bool = False) -> str:
            res = Pipeline(llm=LLM(), options=Options(simulate=simulate)).run(load_document(path))
            files = write_reports(res, output, pdf=pdf, full=full)
            return json.dumps({**_summary(res), "files": {k: str(v.resolve()) for k, v in files.items()}}, ensure_ascii=False, indent=1)

    return mcp
