"""Нейтральная модель отчёта: список блоков, из которого рендерятся MD, HTML и PDF."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class H:
    level: int
    text: str
    anchor: str | None = None


@dataclass
class P:
    text: str  # поддерживается **жирный**


@dataclass
class Q:
    text: str  # цитата из документа


@dataclass
class Callout:
    kind: str  # ok | warn | bad | info
    title: str
    text: str


@dataclass
class Table:
    headers: list[str]
    rows: list[list[str]]
    widths: list[float] | None = None  # доли ширины для PDF
    small: bool = False


@dataclass
class Bullets:
    items: list[str]
    ordered: bool = False


@dataclass
class Code:
    text: str
    lang: str = ""


@dataclass
class FindingCard:
    id: str
    severity: str  # critical|major|minor
    title: str  # критерий
    fields: list[tuple[str, str]] = field(default_factory=list)  # (подпись, текст)
    quotes: list[tuple[str, str]] = field(default_factory=list)  # (цитата, место)


Block = H | P | Q | Callout | Table | Bullets | Code | FindingCard
