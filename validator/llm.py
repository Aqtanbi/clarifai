"""Обёртка над LLM: провайдеры, JSON-ответы, повторы, кэш, статистика."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .config import Settings

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


@dataclass
class LLMStats:
    calls: int = 0
    cached: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    by_stage: dict[str, int] = field(default_factory=dict)


def _strip_json(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    if not text.startswith(("{", "[")):
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start >= 0:
            text = text[start:]
    return text


class LLM:
    """Единый интерфейс: llm.ask(stage, system, user, schema) -> pydantic-объект."""

    def __init__(self, settings: Settings | None = None, api_key: str | None = None, model: str | None = None):
        self.s = settings or Settings()
        self.stats = LLMStats()
        self.provider = self.s.provider
        self.model = model or self.s.model
        self._client: Any = None
        self._api_key = api_key
        self.log: Callable[[str], None] = lambda msg: None  # подключается конвейером

    # ---------- провайдеры ----------
    def _raw_call(self, system: str, user: str) -> tuple[str, int, int]:
        if self.provider == "gemini":
            from google import genai
            from google.genai import types

            if self._client is None:
                key = self._api_key or self.s.gemini_api_key
                if not key:
                    raise LLMError("Не задан GEMINI_API_KEY (см. .env.example)")
                self._client = genai.Client(
                    api_key=key, http_options=types.HttpOptions(timeout=self.s.request_timeout * 1000)
                )
            resp = self._gemini_generate(types, system, user)
            usage = getattr(resp, "usage_metadata", None)
            return (
                resp.text or "",
                int(getattr(usage, "prompt_token_count", 0) or 0),
                int(getattr(usage, "candidates_token_count", 0) or 0),
            )

        if self.provider in ("openai", "ollama", "openrouter", "groq"):
            from openai import OpenAI

            if self._client is None:
                key = self._api_key or self.s.openai_api_key or "not-needed"
                self._client = OpenAI(api_key=key, base_url=self.s.openai_base_url or None, timeout=self.s.request_timeout)
            resp = self._client.chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            u = resp.usage
            return (
                resp.choices[0].message.content or "",
                int(getattr(u, "prompt_tokens", 0) or 0),
                int(getattr(u, "completion_tokens", 0) or 0),
            )

        raise LLMError(f"Неизвестный LLM_PROVIDER: {self.provider}")

    # Разные модели Gemini принимают разные параметры: при отказе (400 INVALID_ARGUMENT)
    # пробуем следующий, более простой вариант конфигурации и запоминаем рабочий.
    _GEMINI_VARIANTS = [
        {"system": True, "temperature": True, "json": True},
        {"system": True, "temperature": False, "json": True},
        {"system": True, "temperature": False, "json": False},
        {"system": False, "temperature": False, "json": False},
    ]

    def _gemini_generate(self, types, system: str, user: str):
        start = getattr(self, "_gemini_variant", 0)
        last: Exception | None = None
        for i in range(start, len(self._GEMINI_VARIANTS)):
            v = self._GEMINI_VARIANTS[i]
            cfg = types.GenerateContentConfig(
                system_instruction=system if v["system"] else None,
                temperature=0 if v["temperature"] else None,
                response_mime_type="application/json" if v["json"] else None,
            )
            contents = user if v["system"] else f"{system}\n\n{user}"
            try:
                resp = self._client.models.generate_content(model=self.model, contents=contents, config=cfg)
                self._gemini_variant = i
                return resp
            except Exception as e:
                msg = str(e)
                if "404" in msg or "NOT_FOUND" in msg:
                    raise LLMError(
                        f"Модель «{self.model}» недоступна для вашего ключа. Выберите другую из списка "
                        f"`python main.py --list-models` и укажите в .env: GEMINI_MODEL=<имя>.\nОтвет API: {msg[:400]}"
                    ) from e
                if not ("400" in msg or "INVALID_ARGUMENT" in msg):
                    raise  # сетевые ошибки и 429 обрабатываются повтором выше
                last = e
                self.log(f"Модель отклонила параметры запроса (вариант {i + 1}), пробую проще: {msg[:150]}")
        raise LLMError(f"Модель «{self.model}» отклонила запрос во всех вариантах конфигурации: {str(last)[:600]}")

    def _call_with_retry(self, system: str, user: str) -> str:
        delay = 5.0
        last: Exception | None = None
        for attempt in range(self.s.max_retries):
            try:
                t0 = time.time()
                self.log(f"  → запрос к модели ({len(system) + len(user)} символов), ждём ответ…")
                text, tin, tout = self._raw_call(system, user)
                self.log(f"  ← ответ за {time.time() - t0:.0f} с ({tout} ток.)")
                self.stats.seconds += time.time() - t0
                self.stats.calls += 1
                self.stats.input_tokens += tin
                self.stats.output_tokens += tout
                return text
            except LLMError:
                raise
            except Exception as e:  # сетевые ошибки, 429, 5xx
                last = e
                msg = str(e)
                retriable = any(x in msg for x in ("429", "RESOURCE_EXHAUSTED", "500", "502", "503", "504", "UNAVAILABLE",
                                                   "overloaded", "timeout", "Timeout", "timed out", "Connection", "ReadTimeout"))
                if not retriable or attempt == self.s.max_retries - 1:
                    raise LLMError(f"Ошибка LLM: {msg[:500]}") from e
                m = re.search(r"retry[^0-9]{0,20}(\d+(?:\.\d+)?)s", msg, re.I)
                wait = min(float(m.group(1)) + 1 if m else delay, 65)
                kind = "лимит запросов API (429)" if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) else f"сбой: {msg[:120]}"
                self.log(f"  ! {kind}; повтор {attempt + 2}/{self.s.max_retries} через {wait:.0f} с")
                time.sleep(wait)
                delay = min(delay * 2, 60)
        raise LLMError(str(last))

    # ---------- кэш ----------
    def _cache_path(self, system: str, user: str) -> Path:
        h = hashlib.sha256(f"{self.provider}|{self.model}|{system}|{user}".encode()).hexdigest()[:32]
        return Path(self.s.cache_dir) / f"{h}.json"

    def complete_json(self, stage: str, system: str, user: str) -> Any:
        self.stats.by_stage[stage] = self.stats.by_stage.get(stage, 0) + 1
        cp = self._cache_path(system, user)
        if self.s.use_cache and cp.exists():
            self.stats.cached += 1
            return json.loads(cp.read_text(encoding="utf-8"))
        text = self._call_with_retry(system, user)
        try:
            data = json.loads(_strip_json(text))
        except json.JSONDecodeError:
            text = self._call_with_retry(
                system, user + "\n\nПредыдущий ответ был невалидным JSON. Верни ТОЛЬКО валидный JSON без комментариев."
            )
            data = json.loads(_strip_json(text))
        if self.s.use_cache:
            try:
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return data

    def ask(self, stage: str, system: str, user: str, schema: type[T], fix: Callable[[Any], Any] | None = None) -> T:
        """Запросить JSON и провалидировать pydantic-схемой; при ошибке схемы — одна попытка исправления."""
        data = self.complete_json(stage, system, user)
        if fix:
            data = fix(data)
        try:
            return schema.model_validate(data)
        except ValidationError as e:
            retry_user = (
                user
                + "\n\nТвой предыдущий ответ не прошёл валидацию схемы:\n"
                + str(e)[:2000]
                + "\nИсправь и верни ТОЛЬКО валидный JSON по схеме."
            )
            data = self.complete_json(stage + ":fix", system, retry_user)
            if fix:
                data = fix(data)
            return schema.model_validate(data)


def list_models(settings: Settings | None = None) -> list[str]:
    s = settings or Settings()
    if s.provider == "gemini":
        from google import genai

        client = genai.Client(api_key=s.gemini_api_key)
        out = []
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                out.append(m.name.replace("models/", ""))
        return out
    from openai import OpenAI

    client = OpenAI(api_key=s.openai_api_key or "x", base_url=s.openai_base_url or None)
    return [m.id for m in client.models.list()]
