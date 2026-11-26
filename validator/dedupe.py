"""Автоматическая склейка дублей находок (до рецензента).

Разные этапы (проверка требований, набора, сверка противоречий, расчёт, вопросы разработчика) нередко находят один и тот же
дефект. Дубли склеиваются детерминированно, без LLM:
- противоречия (COR / S-CON) — если указывают на одни и те же места документа (те же строки с обеих сторон);
- прочие находки — если у них общий критерий-«семейство», общее требование, пересекаются строки цитат и близок текст проблемы.
Остаётся самая сильная находка (серьёзность, подтверждение расчётом, число цитат); остальные помечаются как дубли.
"""
from __future__ import annotations

from .criteria import SEVERITY_ORDER
from .schemas import Finding

FAMILY = {"COR": "conflict", "S-CON": "conflict", "CMP": "complete", "S-CMP": "complete",
          "UNA": "ambiguous", "S-COM": "ambiguous", "VER": "verify", "S-VAL": "verify"}
SOURCE_RANK = {"simulation": 0, "llm": 1, "rule": 2}
TEXT_SIM = 60  # порог похожести текста проблемы (RapidFuzz token_sort_ratio)


_REQ_RANGES: list[tuple[str, int, int]] = []  # (id, первая строка, последняя строка) — для сравнения мест по требованиям


def _place(q) -> str | None:
    """Место цитаты: id требования, в которое она попадает, иначе номер строки."""
    if not q.line_start:
        return None
    for rid, a, b in _REQ_RANGES:
        if a <= q.line_start <= b:
            return rid
    return f"L{q.line_start}"


def _places(f: Finding) -> set[str]:
    return {p for p in (_place(q) for q in f.quotes) if p}


def _lines(f: Finding) -> set[int]:
    out: set[int] = set()
    for q in f.quotes:
        if q.line_start:
            out.update(range(q.line_start, (q.line_end or q.line_start) + 1))
    return out


def _quote_line_sets(f: Finding) -> list[set[int]]:
    return [set(range(q.line_start, (q.line_end or q.line_start) + 1)) for q in f.quotes if q.line_start]


def _similar(a: str, b: str) -> float:
    try:
        from rapidfuzz import fuzz

        return fuzz.token_sort_ratio(a.lower(), b.lower())
    except Exception:  # pragma: no cover
        return 100.0 if a == b else 0.0


def is_duplicate(a: Finding, b: Finding) -> bool:
    fa, fb = FAMILY.get(a.criterion, a.criterion), FAMILY.get(b.criterion, b.criterion)
    if fa != fb:
        return False
    if fa == "conflict":
        pa, pb = _places(a), _places(b)
        if len(pa) >= 2 and len(pb) >= 2:
            return pa == pb  # одна и та же пара мест документа (с точностью до требования)
        # противоречие с одной стороной — если его место входит в пару другой и текст близок
        if pa and pb and (pa <= pb or pb <= pa):
            return _similar(a.problem, b.problem) >= TEXT_SIM - 15
        return False
    if a.req_ids and b.req_ids and not set(a.req_ids) & set(b.req_ids):
        return False
    if not (_lines(a) & _lines(b)):
        return False
    return _similar(a.problem, b.problem) >= TEXT_SIM


def _strength(f: Finding) -> tuple:
    return (SEVERITY_ORDER[f.severity], SOURCE_RANK.get(f.source, 3), -len(f.quotes), -len(f.problem))


def merge_duplicates(findings: list[Finding], reqs: list | None = None) -> int:
    """Помечает дубли (dropped) и обогащает оставшуюся находку. Возвращает число склеенных."""
    global _REQ_RANGES
    _REQ_RANGES = [(r.id, r.line_start, r.line_end or r.line_start) for r in (reqs or []) if r.line_start]
    live = [f for f in findings if not f.dropped]
    parent = list(range(len(live)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            if find(i) != find(j) and is_duplicate(live[i], live[j]):
                parent[find(j)] = find(i)
    groups: dict[int, list[Finding]] = {}
    for i, f in enumerate(live):
        groups.setdefault(find(i), []).append(f)
    merged = 0
    for g in groups.values():
        if len(g) < 2:
            continue
        g.sort(key=_strength)
        keep, rest = g[0], g[1:]
        for f in rest:
            for rid in f.req_ids:
                if rid not in keep.req_ids:
                    keep.req_ids.append(rid)
            have = {(q.line_start, q.text) for q in keep.quotes}
            for q in f.quotes:
                if (q.line_start, q.text) not in have and len(keep.quotes) < 4 and not any(
                        q.line_start and k.line_start == q.line_start for k in keep.quotes):
                    keep.quotes.append(q)
            if not keep.rewrite and f.rewrite:
                keep.rewrite = f.rewrite
            if not keep.question and f.question:
                keep.question = f.question
            if f.source == "simulation" and not keep.evidence:
                keep.evidence = f.evidence
            f.dropped, f.drop_reason = True, f"Дубликат {keep.id} (склеено автоматически)"
            merged += 1
    return merged
