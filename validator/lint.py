"""Детерминированные проверки без LLM: размытые слова, заглушки, незаполненные шаблоны.

Результат воспроизводим на 100% и не зависит от модели.
"""
from __future__ import annotations

import re

from .parsing import Document
from .schemas import Finding, Quote, Requirement

# (шаблон, критерий, серьёзность, пояснение)
VAGUE = [
    (r"\bбыстр\w*", "быстро — без числового критерия"),
    (r"\bсвоевременн\w*", "своевременно — без срока"),
    (r"\bудобн\w*", "удобно — субъективная оценка"),
    (r"\bинтуитивн\w*", "интуитивно — субъективная оценка"),
    (r"\bпри необходимости\b", "при необходимости — не определено, кто и когда решает"),
    (r"\bпо возможности\b", "по возможности — необязательность"),
    (r"\bи т\.\s?[пд]\.", "открытый перечень («и т.п.», «и т.д.»)"),
    (r"\bи тому подобн\w*", "открытый перечень"),
    (r"\bи др\.", "открытый перечень"),
    (r"\bдостаточн\w*", "достаточно — без меры"),
    (r"\bоптимальн\w*", "оптимально — без критерия"),
    (r"\bэффективн\w*", "эффективно — без метрики"),
    (r"\bмаксимально\b", "максимально — без числа"),
    (r"\bгибк\w*", "гибко — без определения"),
    (r"\bнадёжн\w*|\bнадежн\w*", "надёжно — без метрики доступности"),
    (r"\bкак правило\b|\bобычно\b", "допускает исключения без их перечня"),
    (r"\bсоответствующ\w*", "«соответствующий» — не указано, какой именно"),
    (r"\bнекотор\w*|\bразличн\w*", "неопределённое множество"),
    (r"\bи/или\b", "«и/или» — неясно, обязательны ли оба условия"),
    (r"\bв момент\b", "«в момент» — нет допустимой задержки"),
    (r"\bпримерно\b|\bоколо\b|\bпорядка\b", "приблизительное значение"),
]
PLACEHOLDERS = [
    (r"\bTBD\b|\bTODO\b|\bуточнить\b|\bуточняется\b", "Заглушка вместо требования"),
    (r"заполняются\s+\w+|не заполнен\w*", "Раздел не заполнен"),
    (r"Да\s*/\s*Нет", "Чек-лист/таблица оставлены шаблонными (варианты «Да / Нет» не выбраны)"),
]
EMPTY_TABLE_ROW = re.compile(r"^\|(\s*\|)+\s*$")
DASH_VALUE = re.compile(r"(?:\|\s*|:\s*)[—–-]\s*(?:\||$|\.)")


def _req_for_line(reqs: list[Requirement], line: int) -> list[str]:
    return [r.id for r in reqs if r.line_start and r.line_start <= line <= (r.line_end or r.line_start)]


def run_lint(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    findings: list[Finding] = []
    req_lines = {n for r in reqs if r.line_start for n in range(r.line_start, (r.line_end or r.line_start) + 1)}

    # 1. размытые слова — только внутри строк, отнесённых к требованиям
    for line in doc.lines:
        if line.no not in req_lines:
            continue
        for pat, why in VAGUE:
            m = re.search(pat, line.text, re.I)
            if m:
                findings.append(Finding(
                    scope="lint", source="rule", criterion="UNA", severity="minor",
                    req_ids=_req_for_line(reqs, line.no),
                    quotes=[Quote(text=line.text[max(0, m.start() - 40): m.end() + 40].strip(" |*"), line_start=line.no, line_end=line.no)],
                    problem=f"Размытая формулировка: «{m.group(0)}» — {why}.",
                    recommendation="Заменить на измеримое условие (число, единица, срок, перечень).",
                ))

    # 2. заглушки и шаблонные значения — агрегируем по шаблону
    for pat, why in PLACEHOLDERS:
        hits = [l for l in doc.lines if re.search(pat, l.text, re.I)]
        if hits:
            first = hits[0]
            m = re.search(pat, first.text, re.I)
            lines = ", ".join(str(h.no) for h in hits[:15]) + (" …" if len(hits) > 15 else "")
            findings.append(Finding(
                scope="lint", source="rule", criterion="S-CMP", severity="major" if len(hits) > 1 or "заполн" in pat else "minor",
                req_ids=sorted({rid for h in hits for rid in _req_for_line(reqs, h.no)}),
                quotes=[Quote(text=first.text[max(0, m.start() - 60): m.end() + 60].strip(" |*"), line_start=first.no, line_end=first.no)],
                problem=f"{why}: найдено {len(hits)} вхождений (строки {lines}).",
                recommendation="Заполнить раздел или явно указать «не применимо» с обоснованием.",
            ))

    # 3. значения «—» в таблицах параметров (пустые поля)
    dash = [l for l in doc.lines if l.text.lstrip().startswith("|") and DASH_VALUE.search(l.text)]
    if dash:
        findings.append(Finding(
            scope="lint", source="rule", criterion="S-CMP", severity="minor",
            req_ids=sorted({rid for h in dash for rid in _req_for_line(reqs, h.no)}),
            quotes=[Quote(text=dash[0].text.strip(" |*"), line_start=dash[0].no, line_end=dash[0].no)],
            problem=f"Поля таблиц заполнены прочерком «—» без пояснения: строки {', '.join(str(h.no) for h in dash[:15])}.",
            recommendation="Указать значение или причину отсутствия (не применимо / будет определено до даты).",
        ))

    # 4. полностью пустые строки таблиц (например, блок согласующих)
    empty = [l for l in doc.lines if EMPTY_TABLE_ROW.match(l.text.strip())]
    if empty:
        prev = doc.line(empty[0].no - 2)
        findings.append(Finding(
            scope="lint", source="rule", criterion="S-CMP", severity="major",
            quotes=[Quote(text=(prev.text if prev else empty[0].text).strip(" |*"), line_start=empty[0].no - 2, line_end=empty[0].no - 2)],
            problem=f"Пустые строки таблиц (строки {', '.join(str(h.no) for h in empty)}) — таблица не заполнена.",
            recommendation="Заполнить таблицу (ответственные, согласования) до передачи в разработку.",
        ))
    return findings
