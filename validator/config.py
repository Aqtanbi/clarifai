"""Конфигурация из переменных окружения / файла .env."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

try:
    from dotenv import load_dotenv

    # .env проекта главнее переменных окружения оболочки: иначе забытая $env:GEMINI_MODEL молча перекрывает настройку
    load_dotenv(ENV_FILE, override=True)
except Exception:  # pragma: no cover
    pass


def _default_cache_dir() -> str:
    # На Vercel файловая система только для чтения, кроме /tmp
    if os.getenv("VERCEL"):
        return str(Path(tempfile.gettempdir()) / "llm-cache")
    return str(ROOT / ".cache" / "llm")


@dataclass
class Settings:
    provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "gemini").lower())
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.8-flash"))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", ""))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    cache_dir: str = field(default_factory=lambda: os.getenv("LLM_CACHE_DIR", _default_cache_dir()))
    use_cache: bool = field(default_factory=lambda: os.getenv("LLM_CACHE", "1") != "0")
    batch_size: int = field(default_factory=lambda: int(os.getenv("BATCH_SIZE", "8")))
    sim_timeout: int = field(default_factory=lambda: int(os.getenv("SIM_TIMEOUT", "20")))
    max_retries: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_RETRIES", "6")))
    request_timeout: int = field(default_factory=lambda: int(os.getenv("LLM_TIMEOUT", "240")))  # секунд на один запрос

    @property
    def model(self) -> str:
        return self.gemini_model if self.provider == "gemini" else self.openai_model
