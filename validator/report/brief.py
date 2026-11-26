"""Краткий отчёт (режим по умолчанию): резюме на первой странице, таблицы вместо повторяющихся карточек,
подробные карточки только для критичных и существенных находок. Все разделы, обязательные по ТЗ, сохранены."""
from __future__ import annotations

from collections import Counter

from ..criteria import ALL, INDIVIDUAL, SET, SEVERITY
from ..pipeline import RunResult
from ..schemas import Finding
from .blocks import H, P, Bullets, Callout, FindingCard, Table
from .build import KIND, MARK, READY, _short

SEV_SHORT = {"critical": "Крит.", "major": "Сущ.", "minor": "Незн."}
TOP_N = 5


def _ref(q_or_req, doc_id: str | None = None) -> str:
    """Короткая ссылка на место: «Ф-2 · Функциональные требования, строка 38»."""
    section = (getattr(q_or_req, "location", None) or "")
    line = getattr(q_or_req, "line_start", None)
    sec = section.split(", строк")[0].split(", с. ")[0].split(" › ")[-1] if section else ""
    page = ""
    if ", с. " in section:
        page = "с. " + section.split(", с. ")[1].split(",")[0] + ", "
    parts = [p for p in (doc_id, sec) if p]
    tail = f"{page}строка {line}" if line else ""
    return " · ".join(parts) + (", " if parts and tail else "") + tail


def _reqs_label(f: Finding, reqs_by_id: dict) -> str:
    out = []
    for rid in f.req_ids[:4]:
        x = reqs_by_id.get(rid)
        out.append(f"{rid} ({x.doc_id})" if x and x.doc_id else rid)
    return ", ".join(out) + ("…" if len(f.req_ids) > 4 else "") if out else "набор в целом"


def _crit_name(code: str) -> str:
    c = ALL.get(code)
    return c.name if c else code


def _compact_card(f: Finding, reqs_by_id: dict) -> FindingCard:
    fields = [("Проблема", f.problem)]
    fix = f.rewrite or f.recommendation
    if fix:
        fields.append(("Предлагаемая формулировка" if f.rewrite else "Исправить", fix))
    if f.question and f.severity == "critical":
        fields.append(("Вопрос владельцу", f.question))
    if f.source == "simulation":
        fields.append(("Подтверждение", "расчётом на исполняемой модели (раздел 5)"))
    quotes = [(_short(q.text, 220), _ref(q)) for q in f.quotes[:2]]
    title = f"{_crit_name(f.criterion)} · {_reqs_label(f, reqs_by_id)}"
    return FindingCard(id=f.id, severity=f.severity, title=title, fields=fields, quotes=quotes)


def _minor_table(fs: list[Finding], reqs_by_id: dict) -> Table:
    return Table(["ID", "Требование", "Критерий", "Замечание", "Место"],
                 [[f.id, _reqs_label(f, reqs_by_id), _crit_name(f.criterion), _short(f.problem, 200),
                   _ref(f.quotes[0]) if f.quotes else "—"] for f in fs],
                 widths=[0.08, 0.14, 0.15, 0.43, 0.2], small=True)


def build_brief(r: RunResult) -> list:
    b: list = []
    reqs_by_id = {x.id: x for x in r.requirements}
    active = r.active
    sev = Counter(f.severity for f in active)
    ready_kind, ready_text = READY.get(r.verdict.ready, READY["нет"])
    gs = r.grounding
    codes = [c.code for c in INDIVIDUAL]

    # ---------- Резюме (стр. 1) ----------
    b.append(H(1, f"Отчёт о проверке требований: {r.doc.title}"))
    b.append(Callout(ready_kind, f"Итог: {ready_text}", r.verdict.summary))
    b.append(Table(["Требований", "Нарушений", "Критичных", "Существенных", "Незначительных", "Цитат подтверждено"],
                   [[str(len(r.requirements)), str(len(active)), str(sev["critical"]), str(sev["major"]), str(sev["minor"]),
                     f"{gs.get('exact', 0) + gs.get('fuzzy', 0)}/{gs.get('total', 0)}"]]))
    top = [f for f in active if f.severity in ("critical", "major")][:TOP_N]
    if top:
        b.append(H(3, "Главные дефекты"))
        b.append(Bullets([f"**{f.id}** [{SEVERITY[f.severity]}] {_crit_name(f.criterion)} · {_reqs_label(f, reqs_by_id)}: "
                          f"{_short(f.problem, 230)}" for f in top]))
    if r.verdict.top_fixes:
        b.append(H(3, "Что исправить в первую очередь"))
        b.append(Bullets([f"{t.action}" + (f" ({', '.join(t.finding_ids)})" if t.finding_ids else "") for t in r.verdict.top_fixes],
                         ordered=True))
    b.append(P("Краткая версия отчёта: незначительные замечания приведены одной строкой, требования без нарушений — только в матрице. "
               "Полная версия — с параметром --full (на сайте — галочка «Полный отчёт»)."))

    # ---------- 1. Паспорт ----------
    p = r.passport
    b.append(H(2, "1. Паспорт прогона"))
    rows = [
        ["Документ", f"{p['document']} ({p['file']}, {p['format']})"],
        ["Дата", p["date"]],
        ["Модель", p["model"]],
        ["Версия агента", p["agent_version"]],
        ["Прогон", f"{p['llm_calls']} обращений к модели, {p['duration']}"],
    ]
    if r.profile:
        rows.append(["Тип документа (определён агентом)", f"{r.profile.doc_type}; {r.profile.domain}"])
        if r.profile.focus_checks:
            rows.append(["Проверки под этот документ", "; ".join(c.check for c in r.profile.focus_checks)])
    b.append(Table(["Параметр", "Значение"], rows, widths=[0.28, 0.72], small=True))

    # ---------- 2. Нарезка ----------
    b.append(H(2, "2. Как агент нарезал требования"))
    b.append(Table(["ID", "ID в документе", "Тип", "Краткая формулировка", "Цитата"], [
        [x.id, x.doc_id or "—", KIND.get(x.kind, x.kind), x.summary, f"«{_short(x.quote, 110)}», строка {x.line_start or '—'}"]
        for x in r.requirements
    ], widths=[0.07, 0.1, 0.1, 0.33, 0.4], small=True))

    # ---------- 3. Каждое требование ----------
    b.append(H(2, "3. Проверка каждого требования"))
    b.append(P("✓ — соответствует, ✗ — нарушение, — — неприменимо. Критерии ISO/IEC/IEEE 29148 п. 5.2.5."))
    live = {}
    for f in active:
        for rid in f.req_ids:
            live.setdefault(rid, set()).add(f.criterion)
    rows = []
    for x in r.requirements:
        a = r.assessments.get(x.id)
        marks = a.marks if a else {}
        lv = live.get(x.id, set())
        rows.append([x.id + (f" ({x.doc_id})" if x.doc_id else "")]
                    + ["✗" if c in lv else ("✓" if marks.get(c) in ("ok", "fail") else "—") for c in codes])
    b.append(Table(["Требование"] + [ALL[c].name for c in codes], rows, widths=[0.13] + [0.0966] * 9, small=True))
    req_f = [f for f in active if f.scope in ("requirement", "lint") and f.criterion in codes]
    big = [f for f in req_f if f.severity != "minor"]
    small = [f for f in req_f if f.severity == "minor"]
    if big:
        b.append(H(3, "Критичные и существенные нарушения"))
        b.extend(_compact_card(f, reqs_by_id) for f in big)
    if small:
        b.append(H(3, "Незначительные замечания"))
        b.append(_minor_table(small, reqs_by_id))
    if not req_f:
        b.append(P("Нарушений по критериям отдельного требования не выявлено."))

    # ---------- 4. Набор ----------
    b.append(H(2, "4. Проверка всего набора требований"))
    set_f = [f for f in active if f.scope == "set" or (f.scope == "lint" and f.criterion not in codes)]
    per = Counter(f.criterion for f in set_f)
    b.append(Table(["Критерий набора", "Источник", "Нарушений"],
                   [[c.name, c.source, str(per.get(c.code, 0)) if per.get(c.code) else "✓ нет"] for c in SET],
                   widths=[0.35, 0.45, 0.2], small=True))
    big = [f for f in set_f if f.severity != "minor"]
    small = [f for f in set_f if f.severity == "minor"]
    b.extend(_compact_card(f, reqs_by_id) for f in big)
    if small:
        b.append(_minor_table(small, reqs_by_id))
    uncovered = [c for c in r.coverage if c.req_id in reqs_by_id and reqs_by_id[c.req_id].kind != "acceptance" and not c.covered_by]
    if r.coverage:
        lab = ", ".join(c.req_id + (f" ({reqs_by_id[c.req_id].doc_id})" if reqs_by_id[c.req_id].doc_id else "") for c in uncovered)
        b.append(P(f"**Трассируемость:** не покрыты ни одним критерием приёмки — {len(uncovered)} требований" + (f": {lab}." if lab else ".")))
    if r.glossary_gaps:
        b.append(P("**Неопределённые термины:** " + "; ".join(_short(g, 80) for g in r.glossary_gaps) + "."))

    # ---------- 5. Расчёт ----------
    b.append(H(2, "5. Проверка расчётом"))
    sim, out = r.simulation, r.simulation.get("output")
    if sim.get("status") == "ok" and out:
        checks = [(res, c) for res in out.results for c in res.checks]
        literal = {v.name for v in out.variants if v.literal} or {"literal"}
        failed = [(res, c) for res, c in checks if not c.ok and res.variant in literal]
        b.append(P(f"Агент построил исполняемую модель правил документа и прогнал **{len(out.results)}** сценариев "
                   f"(**{len(checks)}** проверок). Расхождений при буквальном прочтении: **{len(failed)}**."))
        if failed:
            b.append(Table(["Сценарий", "Источник", "Документ ожидает", "Получается по правилам"],
                           [[_short(res.title, 80), c.source, c.expected, c.actual] for res, c in failed],
                           widths=[0.34, 0.16, 0.25, 0.25], small=True))
    elif sim.get("status") == "disabled":
        b.append(P("Этап отключён параметром запуска."))
    else:
        b.append(P(f"Не выполнялась: {_short(sim.get('reason') or 'ошибка выполнения', 300)}"))

    # ---------- 6. Сводка ----------
    b.append(H(2, "6. Сводка"))
    crit = [f for f in active if f.severity == "critical"]
    groups = [("Непроверяемые", {"VER", "S-VAL"}), ("Противоречивые", {"COR", "S-CON"}), ("Неполные", {"CMP", "S-CMP"})]
    rows = []
    used = set()
    for name, cs in groups:
        ids = [f.id for f in crit if f.criterion in cs]
        used |= set(ids)
        rows.append([name, str(len(ids)), ", ".join(ids) or "—"])
    other = [f.id for f in crit if f.id not in used]
    if other:
        rows.append(["Прочие критичные", str(len(other)), ", ".join(other)])
    b.append(P(f"Требований: **{len(r.requirements)}**, с нарушениями: **{len({x for f in active for x in f.req_ids})}**. "
               f"Нарушений: **{len(active)}** (критичных {sev['critical']}, существенных {sev['major']}, незначительных {sev['minor']})."))
    b.append(Table(["Критичные дефекты", "Кол-во", "Находки"], rows, widths=[0.3, 0.12, 0.58], small=True))

    # ---------- 7. Итог ----------
    b.append(H(2, "7. Итог"))
    b.append(Callout(ready_kind, f"Можно ли начинать разработку и тестирование: {ready_text}", r.verdict.summary))
    b.append(P(f"Контроль качества: все цитаты сверены с текстом документа ({gs.get('exact', 0) + gs.get('fuzzy', 0)} из "
               f"{gs.get('total', 0)} подтверждены); находки без подтверждённой цитаты и отклонённые рецензентом "
               f"({len(r.findings) - len(active)}) в отчёт не включены."))
    return b
