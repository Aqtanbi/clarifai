"""Мок LLM для офлайн-тестов конвейера (НЕ для генерации сдаваемого отчёта)."""
from __future__ import annotations

from validator.llm import LLM, LLMStats

SIM_CODE = r'''
import json
from datetime import datetime, timezone, timedelta

ALMATY = timezone(timedelta(hours=5))

def month_by_purchase(purchased_at):
    """Ф-2: «Месяц определяется по дате совершения покупки в Алматы»"""
    return purchased_at.astimezone(ALMATY).strftime("%Y-%m")

def month_by_posting(posted_at):
    return posted_at.astimezone(ALMATY).strftime("%Y-%m")

results = []
p = datetime(2026, 1, 31, 23, 58, tzinfo=ALMATY)
q = datetime(2026, 2, 1, 0, 3, tzinfo=ALMATY)
for variant, fn, arg in (("literal", month_by_purchase, p), ("by_posting_date", month_by_posting, q)):
    m = fn(arg)
    results.append({"scenario_id": "S-02", "title": "Покупка 31.01 23:58, проведена 01.02 00:03", "variant": variant,
                    "checks": [{"source": "КП-2 vs Ф-2", "quote": "Февральский счётчик вырос на 4 000 ₸",
                                "expected": "2026-02", "actual": m, "ok": m == "2026-02"}]})
total = 48500 + 1500
results.append({"scenario_id": "S-01", "title": "Достижение порога 50 000", "variant": "literal",
                "checks": [{"source": "КП-1", "quote": "Итог 50 000 ₸ и один push", "expected": "итог=50000, push=1",
                            "actual": f"итог={total}, push={1 if total >= 50000 else 0}", "ok": True}]})
print(json.dumps({"variants": [{"name": "literal", "literal": True, "description": "месяц по дате совершения", "basis_quote": "по дате совершения покупки"},
                               {"name": "by_posting_date", "literal": False, "description": "месяц по дате проведения", "basis_quote": ""}],
                  "results": results}, ensure_ascii=False))
'''

RESPONSES = {
    "profile": {"doc_type": "ТЗ на доработку + спецификация REST API", "domain": "Банковские карты, уведомления",
                "structure": "Часть A — функциональные требования и критерии приёмки; часть B — API", "numbering": "Ф-1…Ф-4, остальное без номеров",
                "defined_terms": [], "formalizable": True,
                "focus_checks": [{"area": "Периоды", "check": "Границы месяца и часовой пояс", "why": "Ф-2 считает расходы по месяцам"}]},
    "extract": {"requirements": [
        {"id": "R-01", "doc_id": "Ф-1", "kind": "functional", "section": "Функциональные требования", "summary": "Порог 1 000–1 000 000 ₸, по умолчанию выключено",
         "quote": "Держатель активной карты KZT задаёт целый порог от 1 000 до 1 000 000 ₸ включительно.", "line_start": 37, "line_end": 37},
        {"id": "R-02", "doc_id": "Ф-2", "kind": "functional", "section": "Функциональные требования", "summary": "Месяц по дате совершения покупки",
         "quote": "Месяц определяется по дате совершения покупки в Алматы.", "line_start": 38, "line_end": 38},
        {"id": "R-03", "doc_id": "Ф-3", "kind": "functional", "section": "Функциональные требования", "summary": "Один push в месяц",
         "quote": "На карту приходится максимум один такой push за календарный месяц.", "line_start": 39, "line_end": 39},
        {"id": "R-04", "doc_id": None, "kind": "acceptance", "section": "Критерии приемки", "summary": "Покупка на стыке месяцев",
         "quote": "Покупка 31 января в 23:58 на 4 000 ₸", "line_start": 64, "line_end": 64},
        {"id": "R-05", "doc_id": None, "kind": "api", "section": "Часть B", "summary": "Метод GET для сохранения",
         "quote": "REST type GET", "line_start": 104, "line_end": 104},
    ]},
    "individual": {"assessments": [
        {"req_id": "R-01", "marks": {"NEC": "ok", "UNA": "fail", "CMP": "ok", "VER": "ok"}, "comment": "Не определена «активная карта»."},
        {"req_id": "R-02", "marks": {"UNA": "fail", "COR": "fail"}, "comment": "Часовой пояс и дата не однозначны."},
        {"req_id": "R-03", "marks": {"CMP": "ok"}, "comment": "ok"},
        {"req_id": "R-05", "marks": {"APP": "fail"}, "comment": "GET для изменения состояния."},
    ], "findings": [
        {"req_ids": ["R-01"], "criterion": "UNA", "severity": "major", "quotes": [{"text": "Держатель активной карты KZT", "line_start": 37}],
         "problem": "Термин «активная карта» не определён.", "recommendation": "Определить статусы карты.", "rewrite": "Держатель карты в статусе «Активна» ...", "question": "Какие статусы считаются активными?"},
        {"req_ids": ["R-05"], "criterion": "APP", "severity": "major", "quotes": ["REST type GET"],
         "problem": "Для сохранения используется GET.", "recommendation": "Использовать PUT/POST.", "rewrite": None, "question": None},
        {"req_ids": ["R-02"], "criterion": "UNA", "severity": "minor", "quotes": [{"text": "абсолютно выдуманная цитата о биометрии"}],
         "problem": "Галлюцинация для проверки grounding.", "recommendation": "-"},
    ]},
    "set": {"findings": [
        {"req_ids": ["R-03"], "criterion": "S-CON", "severity": "critical",
         "quotes": [{"text": "максимум один такой push за календарный месяц"}, {"text": "следующее достижение вызывает push независимо от истории предыдущей настройки"}],
         "problem": "Ф-3 ограничивает одним push в месяц, Ф-4 требует push после смены порога.", "recommendation": "Определить приоритет правил.", "question": "Нужен ли второй push после смены порога?"},
        {"req_ids": [], "criterion": "S-CON", "severity": "major", "quotes": [{"text": "Для чужой карты возвращается 404"}],
         "problem": "Код 404 не входит в список статусов.", "recommendation": "Добавить 404 в список."},
    ], "coverage": [{"req_id": "R-01", "covered_by": [], "comment": "Границы не проверяются"},
                    {"req_id": "R-02", "covered_by": ["R-04"], "comment": "Противоречит"}],
        "glossary_gaps": ["«активная карта» — Ф-1", "«проведённая покупка» — Ф-2"]},
    "simulate": {"applicable": True, "reason": "Учёт расходов и уведомлений", "code": SIM_CODE},
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
                    "verdict": {"ready": "нет", "summary": "Есть противоречия Ф-2/КП-2 и Ф-3/Ф-4.",
                                "top_fixes": [{"finding_ids": ids[:2], "action": "Устранить противоречия"}]}}
        return RESPONSES[key]
