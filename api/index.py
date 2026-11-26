"""Точка входа Vercel (Python Serverless Function).

vercel.json перенаправляет все адреса сюда, передавая исходный путь параметром __path
(Vercel при rewrite подставляет в функцию путь назначения). Обёртка восстанавливает путь до передачи запроса в FastAPI.
"""
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import app as _fastapi_app  # noqa: E402


async def app(scope, receive, send):
    if scope.get("type") in ("http", "websocket"):
        params = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        path = next((v for k, v in params if k == "__path"), None)
        if path is not None:
            path = "/" + path.lstrip("/")
            rest = [(k, v) for k, v in params if k != "__path"]
            scope = dict(scope, path=path, raw_path=path.encode(), query_string=urlencode(rest).encode())
    await _fastapi_app(scope, receive, send)
