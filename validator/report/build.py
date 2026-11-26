"""Сборка содержимого отчёта (на русском) из результата прогона."""
from __future__ import annotations

from collections import Counter

from ..criteria import ALL, INDIVIDUAL, SET, SEVERITY, label
from ..pipeline import RunResult
from ..schemas import Finding
from .blocks import H, P, Q, Bullets, Callout, Code, FindingCard, Table

MARK = {"ok": "✓", "fail": "✗", "na": "—"}
READY = {
    "да": ("ok", "Да — можно начинать разработку и тестирование"),
    "условно": ("warn", "Условно — можно начинать после исправления критичных дефектов"),
    "нет": ("bad", "Нет — начинать разработку и тестирование нельзя"),
}
KIND = {"functional": "ФТ", "nonfunctional": "НФТ", "data": "Данные", "integration": "Интеграция", "api": "API",
        "ui": "UI", "acceptance": "Критерий приёмки", "design": "Проектное", "other": "Прочее"}


def _short(s: str, n: int = 160) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _card(f: Finding, reqs_by_id: dict) -> FindingCard:
    reqs = ", ".join(f"{r} ({reqs_by_id[r].doc_id})" if r in reqs_by_id and reqs_by_id[r].doc_id else r for r in f.req_ids)
    src = {"llm": "LLM", "rule": "правило (детерминированно)", "simulation": "подтверждено расчётом"}[f.source]
    fields = [("Требования", reqs or "набор в целом"), ("Источник", src), ("Проблема", f.problem)]
    if f.recommendation:
        fields.append(("Рекомендация", f.recommendation))
    if f.rewrite:
        fields.append(("Предлагаемая формулировка", f.rewrite))
    if f.question:
        fields.append(("Вопрос владельцу", f.question))
    quotes = [(q.text, (q.location or "") + (" (цитата уточнена по тексту)" if q.match == "fuzzy" else "")) for q in f.quotes]
    return FindingCard(id=f.id, severity=f.severity, title=label(f.criterion), fields=fields, quotes=quotes)


def build_report(r: RunResult) -> list:
    b: list = []
    reqs_by_id = {x.id: x for x in r.requirements}
    active = r.active
    sev = Counter(f.severity for f in active)
    ready_kind, ready_text = READY.get(r.verdict.ready, READY["нет"])
    gs = r.grounding

    b.append(H(1, f"Отчёт о проверке требований: {r.doc.title}"))
    b.append(Callout(ready_kind, f"Итог: {ready_text}",
                     f"Требований: **{len(r.requirements)}**. Нарушений: **{len(active)}** "
                     f"(критичных — **{sev['critical']}**, существенных — **{sev['major']}**, незначительных — **{sev['minor']}**). "
                     f"Цитат проверено по тексту документа: **{gs.get('exact', 0) + gs.get('fuzzy', 0)}/{gs.get('total', 0)}**."))

    # 1. Паспорт
    p = r.passport
    b.append(H(2, "1. Паспорт прогона"))
    b.append(Table(["Параметр", "Значение"], [
        ["Документ", p["document"]],
        ["Файл", f"{p['file']} ({p['format']}, {p['lines']} строк, SHA-256 {p['sha256']}…)"],
        ["Дата и время прогона", p["date"]],
        ["Модель", f"{p['model']} (провайдер: {p['provider']})"],
        ["Версия агента", p["agent_version"]],
        ["Обращений к модели", f"{p['llm_calls']} (из кэша: {p['llm_cached']}); токены: {p['tokens']}"],
        ["Длительность", p["duration"] + ("; " + ", ".join(f"{k} — {v} с" for k, v in r.timings.items()) if r.timings else "")],
        ["Стандарты", "ISO/IEC/IEEE 29148:2018 п. 5.2.5, 5.2.6; IEEE Std 830-1998 п. 4.3"],
    ], widths=[0.28, 0.72]))

    # 2. Нарезка
    b.append(H(2, "2. Как агент нарезал требования"))
    b.append(P("Идентификатор в отчёте присвоен агентом (R-xx). Если в документе номера не было — указано «—», "
               "а требование однозначно определяется цитатой и местом в документе."))
    b.append(Table(["ID в отчёте", "ID в документе", "Тип", "Краткая формулировка", "Цитата и место"], [
        [x.id, x.doc_id or "—", KIND.get(x.kind, x.kind), x.summary,
         f"«{_short(x.quote, 220)}» — {x.location or '—'}" + ("" if x.quote_match in ("exact", "fuzzy") else " ⚠ цитата не найдена дословно")]
        for x in r.requirements
    ], widths=[0.09, 0.11, 0.1, 0.27, 0.43], small=True))

    # 3. Проверка каждого требования
    b.append(H(2, "3. Проверка каждого требования"))
    b.append(P("Матрица соответствия критериям ISO/IEC/IEEE 29148 п. 5.2.5: ✓ — соответствует, ✗ — нарушение (см. находки ниже), — — неприменимо."))
    codes = [c.code for c in INDIVIDUAL]
    rows = []
    for x in r.requirements:
        a = r.assessments.get(x.id)
        marks = a.marks if a else {}
        # нарушение, отброшенное рецензентом/grounding, не должно оставаться ✗
        live = {f.criterion for f in active if x.id in f.req_ids}
        rows.append([x.id] + [MARK["fail"] if c in live else (MARK["ok"] if marks.get(c) == "fail" else MARK.get(marks.get(c, "na"), "—")) for c in codes]
                    + [str(sum(1 for f in active if x.id in f.req_ids))])
    b.append(Table(["ID"] + [ALL[c].name for c in codes] + ["Находок"], rows,
                   widths=[0.08] + [0.091] * 9 + [0.1], small=True))

    by_req: dict[str, list[Finding]] = {}
    for f in active:
        if f.scope in ("requirement",) or (f.scope == "lint" and f.criterion in codes):
            for rid in f.req_ids[:1]:
                by_req.setdefault(rid, []).append(f)
    for x in r.requirements:
        fs = by_req.get(x.id, [])
        a = r.assessments.get(x.id)
        head = f"{x.id}" + (f" ({x.doc_id})" if x.doc_id else "") + f" — {x.summary}"
        b.append(H(3, head))
        b.append(Q(f"{x.quote}  — {x.location or ''}"))
        if a and a.comment:
            b.append(P(f"**Оценка:** {a.comment}"))
        if not fs:
            b.append(P("Нарушений по критериям отдельного требования не выявлено."))
        for f in fs:
            b.append(_card(f, reqs_by_id))

    # 4. Проверка набора
    b.append(H(2, "4. Проверка всего набора требований"))
    set_f = [f for f in active if f.scope == "set" or (f.scope == "lint" and f.criterion not in codes)]
    for c in SET:
        fs = [f for f in set_f if f.criterion == c.code]
        b.append(H(3, f"4.{SET.index(c) + 1}. {c.name} ({c.source})"))
        if not fs:
            b.append(P("Нарушений не выявлено."))
        for f in fs:
            b.append(_card(f, reqs_by_id))
    other = [f for f in set_f if f.criterion not in {c.code for c in SET}]
    for f in other:
        b.append(_card(f, reqs_by_id))

    b.append(H(3, f"4.{len(SET) + 1}. Трассируемость: требования → критерии приёмки"))
    if r.coverage:
        cov_rows = []
        for c in r.coverage:
            if c.req_id not in reqs_by_id or reqs_by_id[c.req_id].kind == "acceptance":
                continue
            x = reqs_by_id[c.req_id]
            cov_rows.append([x.id + (f" ({x.doc_id})" if x.doc_id else ""), _short(x.summary, 80),
                             ", ".join(c.covered_by) if c.covered_by else "✗ не покрыто", c.comment])
        uncovered = sum(1 for row in cov_rows if row[2].startswith("✗"))
        b.append(P(f"Не покрыто ни одним критерием приёмки: **{uncovered} из {len(cov_rows)}** требований."))
        b.append(Table(["Требование", "Суть", "Критерии приёмки", "Комментарий"], cov_rows, widths=[0.14, 0.33, 0.18, 0.35], small=True))
    else:
        b.append(P("Матрица трассируемости не сформирована."))
    if r.glossary_gaps:
        b.append(H(3, f"4.{len(SET) + 2}. Неопределённые термины"))
        b.append(Bullets(r.glossary_gaps))

    # 5. Симуляция
    b.append(H(2, "5. Проверка расчётом (исполняемая модель требований)"))
    sim = r.simulation
    b.append(P("Агент переводит правила документа в исполняемую модель, прогоняет критерии приёмки, граничные значения и "
               "сценарии на стыке правил и сравнивает ожидаемый документом результат с вычисленным. Расхождение — "
               "противоречие, доказанное расчётом; прохождение сценария только при альтернативном прочтении — неоднозначность."))
    out = sim.get("output")
    if sim.get("status") == "ok" and out:
        if out.variants:
            b.append(Table(["Вариант прочтения", "Буквальный", "Описание", "Основание"],
                           [[v.name, "да" if v.literal else "нет", v.description, _short(v.basis_quote, 150)] for v in out.variants],
                           widths=[0.16, 0.1, 0.4, 0.34], small=True))
        rows = []
        for res in out.results:
            for c in res.checks:
                rows.append([res.scenario_id, _short(res.title, 90), res.variant, c.source, c.expected, c.actual, "✓" if c.ok else "✗"])
        total = len(rows)
        failed = sum(1 for x in rows if x[-1] == "✗")
        b.append(P(f"Сценариев: **{len(out.results)}**, проверок: **{total}**, расхождений: **{failed}**. Модель: {sim.get('reason', '')}"))
        b.append(Table(["ID", "Сценарий", "Вариант", "Источник", "Ожидается", "Получено", ""], rows,
                       widths=[0.07, 0.25, 0.1, 0.12, 0.19, 0.19, 0.05], small=True))
        sf = [f for f in active if f.scope == "simulation"]
        if sf:
            b.append(H(3, "Дефекты, подтверждённые расчётом"))
            for f in sf:
                b.append(_card(f, reqs_by_id))
    elif sim.get("status") == "disabled":
        b.append(P("Этап отключён параметром запуска."))
    else:
        b.append(P(f"Модель не построена: {sim.get('reason') or 'ошибка выполнения'}"
                   + (f". Последняя ошибка: {_short(sim['errors'][-1], 300)}" if sim.get("errors") else "")))

    # 6. Сводка
    b.append(H(2, "6. Сводка"))
    crit_by = Counter(f.criterion for f in active)
    b.append(Table(["Показатель", "Значение"], [
        ["Требований выделено", str(len(r.requirements))],
        ["Требований с нарушениями", str(len({rid for f in active for rid in f.req_ids}))],
        ["Нарушений всего", str(len(active))],
        ["Критичных / существенных / незначительных", f"{sev['critical']} / {sev['major']} / {sev['minor']}"],
        ["Подтверждено расчётом", str(sum(1 for f in active if f.scope == 'simulation'))],
        ["Найдено детерминированными правилами", str(sum(1 for f in active if f.scope == 'lint'))],
        ["Отброшено при проверке качества (рецензент, grounding)", str(len(r.findings) - len(active))],
    ], widths=[0.6, 0.4]))
    b.append(Table(["Критерий", "Нарушений"], [[label(k), str(v)] for k, v in crit_by.most_common()], widths=[0.8, 0.2], small=True))

    groups = [
        ("Непроверяемые", {"VER", "S-VAL"}),
        ("Противоречивые", {"COR", "S-CON"}),
        ("Неполные", {"CMP", "S-CMP"}),
        ("Неоднозначные", {"UNA", "S-COM"}),
    ]
    crit = [f for f in active if f.severity == "critical"]
    b.append(H(3, f"Критичные дефекты ({len(crit)})"))
    used = set()
    for name, cset in groups:
        fs = [f for f in crit if f.criterion in cset]
        if fs:
            b.append(P(f"**{name}:**"))
            b.append(Bullets([f"{f.id} [{', '.join(f.req_ids) or 'набор'}] {_short(f.problem, 220)} — {f.quotes[0].location if f.quotes else ''}" for f in fs]))
            used |= {f.id for f in fs}
    rest = [f for f in crit if f.id not in used]
    if rest:
        b.append(P("**Прочие критичные:**"))
        b.append(Bullets([f"{f.id} [{', '.join(f.req_ids) or 'набор'}] {_short(f.problem, 220)}" for f in rest]))

    # 7. Итог
    b.append(H(2, "7. Итог"))
    b.append(Callout(ready_kind, f"Можно ли начинать разработку и тестирование: {ready_text}", r.verdict.summary))
    if r.verdict.top_fixes:
        b.append(H(3, "Что исправить в первую очередь"))
        b.append(Bullets([f"{t.action} ({', '.join(t.finding_ids)})" if t.finding_ids else t.action for t in r.verdict.top_fixes], ordered=True))

    # Приложения
    b.append(H(2, "Приложение A. Критерии проверки"))
    b.append(Table(["Код", "Критерий", "Источник", "Что проверяется"], [[c.code, c.name, c.source, c.question] for c in INDIVIDUAL + SET],
                   widths=[0.08, 0.17, 0.2, 0.55], small=True))

    b.append(H(2, "Приложение B. Контроль качества отчёта"))
    b.append(P(f"Каждая цитата программно сверена с текстом документа. Всего цитат: **{gs.get('total', 0)}**; "
               f"найдено дословно: **{gs.get('exact', 0)}**; найдено с уточнением формулировки (заменено на точный текст документа): "
               f"**{gs.get('fuzzy', 0)}**; не найдено: **{gs.get('not_found', 0)}** — находки без подтверждённой цитаты в отчёт не включены. "
               "Находки дополнительно проверены независимым проходом рецензента (дубликаты, неверные утверждения, общие фразы)."))
    dropped = [f for f in r.findings if f.dropped]
    if dropped:
        b.append(Table(["ID", "Критерий", "Проблема", "Причина исключения"],
                       [[f.id, ALL[f.criterion].name if f.criterion in ALL else f.criterion, _short(f.problem, 200), f.drop_reason or ""] for f in dropped],
                       widths=[0.08, 0.15, 0.47, 0.3], small=True))
    if sim.get("code"):
        b.append(H(2, "Приложение C. Исполняемая модель требований (сгенерирована агентом)"))
        b.append(Code(sim["code"], "python"))
    return b
