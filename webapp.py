"""ClarifAI: веб-интерфейс и HTTP API агента (FastAPI). Локально: `uvicorn webapp:app --reload`; на Vercel — api/index.py.

Маршруты:
  GET  /               — страница загрузки документа
  POST /api/validate   — прогон; ответ — поток NDJSON: события прогресса, затем {"type":"result",...}
  GET  /api/health     — проверка
  /mcp                 — MCP-сервер (Streamable HTTP, stateless)
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from mcp.server.transport_security import TransportSecuritySettings

from validator import __version__
from validator.config import ROOT, Settings
from validator.llm import LLM
from validator.mcp_tools import build_server
from validator.parsing import load_document
from validator.pipeline import Options, Pipeline
from validator.report import render_all

MAX_UPLOAD = 4 * 1024 * 1024  # лимит тела запроса Vercel ~4.5 МБ
ACCESS_CODE = os.getenv("ACCESS_CODE", "")  # если задан — требуется для запуска с серверным ключом
MCP_TOKEN = os.getenv("MCP_TOKEN", "")  # если задан — /mcp требует Authorization: Bearer <MCP_TOKEN>

mcp = build_server(remote=True)
mcp_app = mcp.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@contextlib.asynccontextmanager
async def lifespan(_app):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="ClarifAI", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def mcp_auth(request: Request, call_next):
    if MCP_TOKEN and request.url.path.startswith("/mcp"):
        if request.headers.get("authorization", "") != f"Bearer {MCP_TOKEN}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


app.mount("/mcp", mcp_app)


@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "web" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    s = Settings()
    return {"ok": True, "version": __version__, "provider": s.provider, "model": s.model,
            "server_key": bool(s.gemini_api_key or s.openai_api_key), "access_code_required": bool(ACCESS_CODE)}


@app.post("/api/validate")
async def validate(
    file: UploadFile = File(...),
    simulate: bool = Form(True),
    critic: bool = Form(True),
    full: bool = Form(False),
    api_key: str = Form(""),
    model: str = Form(""),
    access_code: str = Form(""),
):
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        return JSONResponse({"error": "Файл больше 4 МБ"}, status_code=413)
    if not api_key and ACCESS_CODE and access_code != ACCESS_CODE:
        return JSONResponse({"error": "Нужен код доступа или собственный API-ключ"}, status_code=403)
    try:
        doc = load_document(data=data, filename=file.filename or "document.md")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    q: queue.Queue = queue.Queue()

    def progress(stage: str, msg: str):
        q.put({"type": "progress", "stage": stage, "message": msg})

    def work():
        try:
            llm = LLM(Settings(), api_key=api_key or None, model=model or None)
            res = Pipeline(llm=llm, options=Options(simulate=simulate, critic=critic), progress=progress).run(doc)
            progress("render", "Формирую отчёт (MD, HTML, PDF)")
            out = render_all(res, full=full)
            act = res.active
            q.put({"type": "result", "summary": {
                "document": res.doc.title, "requirements": len(res.requirements), "violations": len(act),
                "critical": sum(f.severity == "critical" for f in act), "major": sum(f.severity == "major" for f in act),
                "minor": sum(f.severity == "minor" for f in act), "ready": res.verdict.ready, "verdict": res.verdict.summary,
                "model": res.passport["model"], "grounding": res.grounding},
                "md": out["md"], "html": out["html"], "interactive": out["interactive"], "pdf_base64": base64.b64encode(out["pdf"]).decode()})
        except Exception as e:  # noqa: BLE001
            q.put({"type": "error", "message": str(e)[:1000]})
        finally:
            q.put(None)

    progress("load", f"Документ «{doc.title}»: {len(doc.lines)} строк")
    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield json.dumps({"type": "ping"}) + "\n"  # держим соединение
                continue
            if item is None:
                break
            yield json.dumps(item, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
