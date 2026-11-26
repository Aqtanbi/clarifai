"""Мок LLM для офлайн-тестов конвейера (НЕ для генерации сдаваемого отчёта)."""
from __future__ import annotations

from validator.llm import LLM, LLMStats

SIM_CODE = r"""
import json
from datetime import date

def fine(issued, returned, term=14, per_day=50, cap=1000):
    # П-2/П-3: «Штраф 50 ₸ за каждый полный день просрочки», «не превышает 1 000 ₸»
    late = (returned - issued).days - term
    return min(max(late, 0) * per_day, cap)

results = []
for sid, ret, exp, src, quote in (("S-01", date(2026, 3, 17), 100, "КП-1", "Штраф 100 ₸"),
                                   ("S-02", date(2026, 3, 20), 300, "КП-2", "Штраф 300 ₸")):
    a = fine(date(2026, 3, 1), ret)
    results.append({"scenario_id": sid, "title": f"Возврат {ret.isoformat()}", "variant": "literal",
                    "checks": [{"source": src + " vs П-2", "quote": quote, "expected": str(exp), "actual": str(a), "ok": a == exp}]})
print(json.dumps({"variants": [{"name": "literal", "literal": True, "description": "срок 14 дней", "basis_quote": "14 дней"}],
                  "results": results}, ensure_ascii=False))
"""

RESPONSES = {
    "profile": {"doc_type": "Функциональные требования", "domain": "Библиотека, штрафы",
                "structure": "П-1…П-4, НФТ, критерии приёмки", "numbering": "П-1…П-4", "defined_terms": [], "formalizable": True,
                "focus_checks": [{"area": "Расчёт штрафа", "check": "Границы дней и предел суммы", "why": "П-2 и П-3 задают формулу"}]},
    "extract": {"requirements": [
        {"id": "R-01", "doc_id": "П-1", "kind": "functional", "summary": "До 5 книг", "quote": "Читатель с активным абонементом может взять до 5 книг одновременно.", "line_start": 7},
        {"id": "R-02", "doc_id": "П-2", "kind": "functional", "summary": "Срок 14 дней, штраф 50 ₸/день", "quote": "Срок возврата — 14 дней с даты выдачи по времени Астаны.", "line_start": 8},
        {"id": "R-03", "doc_id": "П-4", "kind": "functional", "summary": "Продление без штрафа", "quote": "При продлении срока штраф не начисляется независимо от истории просрочек.", "line_start": 10},
        {"id": "R-04", "doc_id": None, "kind": "nonfunctional", "summary": "Быстрое уведомление", "quote": "Система быстро уведомляет читателя о штрафе.", "line_start": 14},
        {"id": "R-05", "doc_id": None, "kind": "acceptance", "summary": "Возврат 20 марта", "quote": "Возврат 20 марта", "line_start": 21},
    ]},
    "individual": {"assessments": [
        {"req_id": "R-01", "marks": {"NEC": "ok", "UNA": "fail", "VER": "ok"}, "comment": "Не определён «активный абонемент»."},
        {"req_id": "R-04", "marks": {"VER": "fail"}, "comment": "Нет измеримого срока."},
    ], "findings": [
        {"req_ids": ["R-01"], "criterion": "UNA", "severity": "major", "quotes": [{"text": "с активным абонементом", "line_start": 7}],
         "problem": "Термин «активный абонемент» не определён.", "recommendation": "Определить статусы абонемента.",
         "rewrite": "Читатель с абонементом в статусе «Действует» …", "question": "Какие статусы считаются активными?"},
        {"req_ids": ["R-04"], "criterion": "VER", "severity": "critical", "quotes": ["Система быстро уведомляет читателя"],
         "problem": "«Быстро» не измеримо.", "recommendation": "Указать срок в минутах."},
        {"req_ids": ["R-02"], "criterion": "UNA", "severity": "minor", "quotes": [{"text": "абсолютно выдуманная цитата о биометрии"}],
         "problem": "Галлюцинация для проверки grounding.", "recommendation": "-"},
    ]},
    "set": {"findings": [
        {"req_ids": ["R-03"], "criterion": "S-CON", "severity": "critical",
         "quotes": [{"text": "Штраф 50 ₸ за каждый полный день просрочки"}, {"text": "штраф не начисляется независимо от истории просрочек"}],
         "problem": "П-4 отменяет уже начисленный по П-2 штраф или только будущий — неясно.", "recommendation": "Определить приоритет правил.",
         "question": "Списывается ли накопленный штраф при продлении?"},
    ], "coverage": [{"req_id": "R-01", "covered_by": [], "comment": "Лимит 5 книг не проверяется"}],
        "glossary_gaps": ["«активный абонемент» — П-1"]},
    "simulate": {"applicable": True, "reason": "Расчёт штрафа", "code": SIM_CODE},
    "consistency": {"findings": [
        {"req_ids": ["R-02", "R-05"], "criterion": "S-CON", "severity": "critical",
         "quotes": [{"text": "Штраф 50 ₸ за каждый полный день просрочки"}, {"text": "Штраф 300 ₸"}],
         "problem": "Возврат 20 марта: 19 дней − 14 = 5 дней × 50 ₸ = 250 ₸, а критерий ожидает 300 ₸.", "recommendation": "Согласовать формулу и критерий."}]},
    "probe": {"findings": [
        {"req_ids": ["R-03"], "criterion": "CMP", "severity": "major", "quotes": [{"text": "При продлении срока"}],
         "problem": "Сколько раз можно продлевать срок? Документ не отвечает.", "question": "Есть ли лимит продлений?"}]},
}


class MockLLM(LLM):
    def __init__(self):
        self.provider, self.model, self.stats = "mock", "mock-model", LLMStats()

    def complete_json(self, stage, system, user, schema=None):
        self.stats.calls += 1
        key = stage.split("-")[0].split(":")[0]
        if key == "critic":
            import json, re
            ids = re.findall(r'"id": "(F-\d+)"', user)
            return {"decisions": [{"id": i, "action": "keep"} for i in ids][:-1] + ([{"id": ids[-1], "action": "drop", "reason": "дубликат"}] if ids else []),
                    "verdict": {"ready": "нет", "summary": "Противоречие П-2/КП-2 и неизмеримое НФТ.",
                                "top_fixes": [{"finding_ids": ids[:2], "action": "Устранить противоречия"}]}}
        return RESPONSES[key]
