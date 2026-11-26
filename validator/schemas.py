"""Структуры данных: требования, находки, ответы LLM."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["critical", "major", "minor"]
Mark = Literal["ok", "fail", "na"]


class Quote(BaseModel):
    text: str
    line_start: int | None = None
    line_end: int | None = None
    # заполняется grounding-ом
    match: Literal["exact", "fuzzy", "not_found", "unchecked"] = "unchecked"
    score: float | None = None
    location: str | None = None


# ---------- профиль документа ----------
class FocusCheck(BaseModel):
    area: str = Field(description="Область риска, например «Периоды и часовые пояса»")
    check: str = Field(description="Что именно проверить в этом документе")
    why: str = Field(default="", description="Почему это важно для данного документа (со ссылкой на его содержание)")


class DocProfile(BaseModel):
    doc_type: str = Field(description="Тип документа: ТЗ, SRS, BRD, user stories, API-спецификация, ...")
    domain: str = Field(description="Предметная область")
    structure: str = Field(default="", description="Как устроен документ: части, разделы, где находятся требования")
    numbering: str = Field(default="", description="Схема идентификаторов требований или «нет нумерации»")
    defined_terms: list[str] = Field(default_factory=list, description="Термины, которые определены в документе")
    formalizable: bool = Field(default=True, description="Есть ли правила с числами/условиями/состояниями или примеры, которые можно проверить расчётом")
    focus_checks: list[FocusCheck] = Field(default_factory=list, description="8–15 проверок, специфичных для этого документа")


# ---------- нарезка ----------
class Requirement(BaseModel):
    id: str = Field(description="Идентификатор в отчёте: R-01, R-02, ...")
    doc_id: str | None = Field(default=None, description="Идентификатор в документе, если есть (Ф-1); иначе null")
    kind: str = Field(default="functional", description="functional|nonfunctional|data|integration|api|ui|acceptance|design|other")
    section: str = ""
    summary: str = Field(description="Краткая формулировка своими словами (до 15 слов)")
    quote: str = Field(description="Дословная цитата требования из документа")
    line_start: int | None = None
    line_end: int | None = None
    location: str | None = None
    quote_match: str | None = None


class ExtractResponse(BaseModel):
    requirements: list[Requirement]


# ---------- находки ----------
class Finding(BaseModel):
    id: str = ""
    scope: Literal["requirement", "set", "simulation", "lint"] = "requirement"
    req_ids: list[str] = Field(default_factory=list)
    criterion: str
    severity: Severity = "major"
    quotes: list[Quote] = Field(default_factory=list)
    problem: str
    recommendation: str = ""
    rewrite: str | None = Field(default=None, description="Предлагаемая формулировка требования")
    question: str | None = Field(default=None, description="Вопрос владельцу продукта/аналитику")
    evidence: str | None = None  # для simulation: расчёт
    source: Literal["llm", "rule", "simulation"] = "llm"
    grounded: bool = True
    dropped: bool = False
    drop_reason: str | None = None

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v):
        v = str(v).lower().strip()
        return {"high": "critical", "критично": "critical", "medium": "major", "low": "minor"}.get(v, v)


class Assessment(BaseModel):
    req_id: str
    marks: dict[str, Mark] = Field(default_factory=dict, description="код критерия -> ok|fail|na")
    comment: str = ""


class IndividualResponse(BaseModel):
    assessments: list[Assessment]
    findings: list[Finding] = Field(default_factory=list)


class FindingsResponse(BaseModel):
    findings: list[Finding] = Field(default_factory=list)


class AcCheck(BaseModel):
    ac_id: str = Field(description="id критерия приёмки/примера (R-xx)")
    req_ids: list[str] = Field(default_factory=list, description="id требований, по которым считали")
    steps: str = Field(default="", description="Расчёт по тексту требований, 1–3 шага")
    computed: str = Field(default="", description="Результат по требованиям")
    expected: str = Field(default="", description="Ожидаемый результат по критерию")
    match: bool | None = Field(default=None, description="Совпадают ли computed и expected")
    quote_rule: str = Field(default="", description="Дословная цитата правила, по которому считали")
    quote_ac: str = Field(default="", description="Дословная цитата ожидаемого результата критерия")


class PairCheck(BaseModel):
    req_a: str
    req_b: str
    scenario: str = Field(default="", description="Сценарий, на котором сравниваются требования")
    result_a: str = ""
    result_b: str = ""
    conflict: bool | None = None
    quote_a: str = ""
    quote_b: str = ""


class ConsistencyResponse(BaseModel):
    ac_checks: list[AcCheck] = Field(default_factory=list)
    pair_checks: list[PairCheck] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class Probe(BaseModel):
    req_id: str
    question: str = Field(description="Вопрос разработчика/тестировщика к требованию")
    answered: bool | None = Field(default=None, description="Отвечает ли документ на вопрос однозначно")
    answer_quote: str = Field(default="", description="Дословная цитата ответа из документа, если есть")
    criterion: str = Field(default="CMP", description="CMP|UNA|VER|COR — если ответа нет")
    severity: str = Field(default="major", description="critical|major|minor — если ответа нет")
    why: str = Field(default="", description="Почему ответа нет или он неоднозначен")


class ProbeResponse(BaseModel):
    probes: list[Probe] = Field(default_factory=list)


class Coverage(BaseModel):
    req_id: str
    covered_by: list[str] = Field(default_factory=list, description="ID требований-критериев приёмки, которые проверяют это требование")
    comment: str = ""


class SetResponse(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
    glossary_gaps: list[str] = Field(default_factory=list, description="Термины, которые используются, но не определены")


# ---------- симуляция ----------
class SimulationCode(BaseModel):
    applicable: bool
    reason: str = ""
    code: str = ""


class SimCheck(BaseModel):
    source: str
    quote: str = ""
    expected: str
    actual: str
    ok: bool


class SimResult(BaseModel):
    scenario_id: str
    title: str
    variant: str = "literal"
    checks: list[SimCheck]


class SimVariant(BaseModel):
    name: str
    literal: bool = True
    description: str = ""
    basis_quote: str = ""


class SimOutput(BaseModel):
    variants: list[SimVariant] = Field(default_factory=list)
    results: list[SimResult]


# ---------- критик и итог ----------
class Decision(BaseModel):
    id: str
    action: Literal["keep", "drop"] = "keep"
    severity: Severity | None = None
    reason: str = ""


class TopFix(BaseModel):
    finding_ids: list[str] = Field(default_factory=list)
    action: str


class Verdict(BaseModel):
    ready: Literal["да", "условно", "нет"]
    summary: str
    top_fixes: list[TopFix] = Field(default_factory=list)


class CriticResponse(BaseModel):
    decisions: list[Decision] = Field(default_factory=list)
    verdict: Verdict
