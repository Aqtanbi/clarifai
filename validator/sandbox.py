"""Безопасный запуск кода модели требований, сгенерированного LLM.

1) Статическая проверка AST: только разрешённые модули стандартной библиотеки, без
   файлов/сети/eval/exec/доступа к служебным атрибутам.
2) Запуск в отдельном процессе `python -I` во временной папке, с таймаутом и
   урезанным окружением (без API-ключей). Работает на Windows, Linux, Vercel.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ALLOWED_MODULES = {
    "json", "datetime", "decimal", "fractions", "math", "dataclasses", "enum", "typing",
    "itertools", "collections", "functools", "re", "__future__", "copy", "operator", "statistics",
}
FORBIDDEN_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "input", "globals", "locals", "vars",
    "breakpoint", "help", "exit", "quit", "memoryview", "setattr", "delattr",
}
FORBIDDEN_ATTRS_PREFIX = "__"
ALLOWED_DUNDERS = {"__init__", "__name__", "__post_init__", "__eq__", "__repr__", "__str__", "__lt__", "__hash__", "__doc__", "__main__"}


class UnsafeCode(ValueError):
    pass


def check_code(code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise UnsafeCode(f"SyntaxError: {e}") from e
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_MODULES:
                    raise UnsafeCode(f"Запрещённый импорт: {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_MODULES:
                raise UnsafeCode(f"Запрещённый импорт: {node.module}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise UnsafeCode(f"Запрещённое имя: {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith(FORBIDDEN_ATTRS_PREFIX) and node.attr not in ALLOWED_DUNDERS:
                raise UnsafeCode(f"Запрещённый атрибут: {node.attr}")


@dataclass
class RunResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int | None


def run_code(code: str, timeout: int = 20) -> RunResult:
    check_code(code)
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    # Windows без SYSTEMROOT не стартует python
    for k in ("SYSTEMROOT", "SystemRoot", "PATH", "TEMP", "TMP"):
        if k in os.environ:
            env[k] = os.environ[k]
    with tempfile.TemporaryDirectory(prefix="reqsim_") as td:
        script = Path(td) / "model.py"
        script.write_text(code, encoding="utf-8")
        try:
            p = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", str(script)],
                cwd=td, env=env, capture_output=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return RunResult(False, "", f"Таймаут {timeout} c", None)
        out = p.stdout.decode("utf-8", errors="replace")[:500_000]
        err = p.stderr.decode("utf-8", errors="replace")[-5000:]
        return RunResult(p.returncode == 0, out, err, p.returncode)
