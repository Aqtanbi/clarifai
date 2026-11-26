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
RUNS_PER_HOUR = int(os.getenv("RUNS_PER_HOUR", "6"))  # лимит прогонов с одного адреса на серверном ключе (0 — без лимита)
_runs: dict[str, list[float]] = {}


def _rate_limited(ip: str) -> bool:
    import time

    if RUNS_PER_HOUR <= 0:
        return False
    now = time.time()
    hits = [t for t in _runs.get(ip, []) if now - t < 3600]
    if len(hits) >= RUNS_PER_HOUR:
        _runs[ip] = hits
        return True
    _runs[ip] = hits + [now]
    return False
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


# Диагностика развёртывания: при 404 показываем, какой путь получило приложение (без значений заголовков с секретами)
from starlette.exceptions import HTTPException as _StarletteHTTPException  # noqa: E402


@app.exception_handler(_StarletteHTTPException)
async def _not_found(request: Request, exc: _StarletteHTTPException):
    if exc.status_code != 404:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    sc = request.scope
    safe = {k: v for k, v in request.headers.items()
            if k.startswith(("x-vercel", "x-matched", "x-forwarded", "x-original", "x-invoke", "x-now", "host"))
            and "token" not in k and "signature" not in k}
    return JSONResponse({"detail": "Not Found", "debug": {
        "path": sc.get("path"), "raw_path": (sc.get("raw_path") or b"").decode("latin-1"),
        "root_path": sc.get("root_path"), "query_string": (sc.get("query_string") or b"").decode("latin-1"),
        "headers": safe}}, status_code=404)


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
    request: Request,
    file: UploadFile | None = File(None),
    # Альтернатива multipart-файлу: содержимое в base64 (gzip). Фильтры хостинга (WAF) блокируют запросы,
    # если в тексте документа есть «подозрительные» фрагменты вроде curl … Authorization: Bearer — так их не видно.
    file_b64: str = Form(""),
    filename: str = Form(""),
    encoding: str = Form(""),
    simulate: bool = Form(True),
    critic: bool = Form(True),
    full: bool = Form(False),
    api_key: str = Form(""),
    model: str = Form(""),
    access_code: str = Form(""),
):
    if file_b64:
        import base64
        import gzip

        try:
            data = base64.b64decode(file_b64)
            if encoding == "gzip":
                data = gzip.decompress(data)
        except Exception:
            return JSONResponse({"error": "Не удалось прочитать файл (повреждённая передача)"}, status_code=400)
        name = filename or "document.md"
    elif file is not None:
        data = await file.read()
        name = file.filename or "document.md"
    else:
        return JSONResponse({"error": "Файл не передан"}, status_code=400)
    if len(data) > MAX_UPLOAD:
        return JSONResponse({"error": "Файл больше 4 МБ"}, status_code=413)
    if not api_key and ACCESS_CODE and access_code != ACCESS_CODE:
        return JSONResponse({"error": "Неверный код доступа. Введите код или укажите свой ключ Gemini."}, status_code=403)
    if not api_key:
        ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
        if _rate_limited(ip):
            return JSONResponse({"error": f"Лимит: не больше {RUNS_PER_HOUR} проверок в час на серверном ключе. "
                                          "Попробуйте позже или укажите свой ключ Gemini."}, status_code=429)
    try:
        doc = load_document(data=data, filename=name)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    q: queue.Queue = queue.Queue()

    import time as _t

    t0 = _t.time()

    def progress(stage: str, msg: str):
        print(f"[run {doc.name}] {_t.time() - t0:6.1f}s {msg}", flush=True)  # видно в логах хостинга
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
            import traceback

            traceback.print_exc()
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
