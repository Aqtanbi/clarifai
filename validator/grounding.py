"""Grounding: программная проверка, что каждая цитата действительно есть в документе.

Цитата нормализуется (регистр, ё/е, кавычки, тире, пробелы, разметка markdown) и ищется в
нормализованном тексте документа. Найдено точно → match=exact; иначе нечёткий поиск
(RapidFuzz) → при score ≥ порога цитата ЗАМЕНЯЕТСЯ на точный фрагмент оригинала (match=fuzzy);
иначе match=not_found и находка помечается как необоснованная.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .parsing import Document

FUZZY_THRESHOLD = 85.0
_NEG = {"не", "нет", "ни", "без", "нельзя", "запрещено", "not", "no"}
_NUM = re.compile(r"\d+")


def _guard(quote_norm: str, frag_norm: str) -> bool:
    """Нечёткое совпадение допустимо, только если не потеряны/добавлены отрицания и числа."""
    qw, fw = quote_norm.split(), frag_norm.split()
    if sum(w in _NEG for w in qw) != sum(w in _NEG for w in fw):
        return False
    return set(_NUM.findall(quote_norm.replace(" ", ""))) <= set(_NUM.findall(frag_norm.replace(" ", "")))

_MAP = {
    "ё": "е", "«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "'": '"', "’": '"', "‘": '"',
    "—": "-", "–": "-", "−": "-", "‑": "-", " ": " ", " ": " ", " ": " ", "\t": " ",
}
_DROP = set("*_`|#>\\")


@dataclass
class NormText:
    norm: str
    origin: list[int]  # индекс символа оригинала для каждого символа norm
    line_of: list[int]  # номер строки оригинала для каждого символа norm
    original: str


def normalize(text: str, line_starts: list[int] | None = None) -> NormText:
    out: list[str] = []
    origin: list[int] = []
    line_of: list[int] = []
    line = 1
    prev_space = True
    for i, ch in enumerate(text):
        if ch == "\n":
            line_no_for_space = line
            line += 1
            if not prev_space:
                out.append(" ")
                origin.append(i)
                line_of.append(line_no_for_space)
                prev_space = True
            continue
        c = _MAP.get(ch, ch).lower()
        if c in _DROP:
            continue
        if c.isspace():
            if prev_space:
                continue
            c = " "
            prev_space = True
        else:
            prev_space = False
        out.append(c)
        origin.append(i)
        line_of.append(line)
    return NormText("".join(out), origin, line_of, text)


def _norm_quote(q: str) -> str:
    return normalize(q).norm.strip()


class Grounder:
    def __init__(self, doc: Document):
        self.doc = doc
        self.nt = normalize(doc.text)
        self.stats = {"total": 0, "exact": 0, "fuzzy": 0, "not_found": 0}

    def _span_to_result(self, s: int, e: int) -> tuple[str, int, int]:
        e = max(s, min(e, len(self.nt.norm)) - 1)
        o1, o2 = self.nt.origin[s], self.nt.origin[e]
        frag = self.nt.original[o1 : o2 + 1]
        frag = " ".join(x.strip(" |*#") for x in frag.splitlines() if x.strip(" |*#"))
        frag = re.sub(r"\*\*|`", "", frag)
        return re.sub(r"\s+", " ", frag).strip(), self.nt.line_of[s], self.nt.line_of[e]

    def locate(self, quote: str) -> dict:
        """Вернуть {match, score, text, line_start, line_end, location}."""
        self.stats["total"] += 1
        q = _norm_quote(quote)
        if len(q) < 3:
            self.stats["not_found"] += 1
            return {"match": "not_found", "score": 0.0}
        idx = self.nt.norm.find(q)
        if idx >= 0:
            text, l1, l2 = self._span_to_result(idx, idx + len(q))
            self.stats["exact"] += 1
            return {"match": "exact", "score": 100.0, "text": text, "line_start": l1, "line_end": l2,
                    "location": self.doc.location(l1, l2)}
        try:
            from rapidfuzz import fuzz

            al = fuzz.partial_ratio_alignment(q, self.nt.norm, score_cutoff=FUZZY_THRESHOLD)
        except Exception:
            al = None
        if al and al.score >= FUZZY_THRESHOLD:
            # расширяем до границ слов
            s0, e0 = al.dest_start, al.dest_end
            while s0 > 0 and self.nt.norm[s0 - 1] != " ":
                s0 -= 1
            while e0 < len(self.nt.norm) and self.nt.norm[e0] not in " .,;:":
                e0 += 1
            al.dest_start, al.dest_end = s0, e0
            if not _guard(q, self.nt.norm[s0:e0]):
                al = None
        if al and al.score >= FUZZY_THRESHOLD:
            text, l1, l2 = self._span_to_result(al.dest_start, al.dest_end)
            self.stats["fuzzy"] += 1
            return {"match": "fuzzy", "score": round(al.score, 1), "text": text, "line_start": l1, "line_end": l2,
                    "location": self.doc.location(l1, l2)}
        self.stats["not_found"] += 1
        return {"match": "not_found", "score": round(al.score, 1) if al else 0.0}

    def ground_quote(self, q) -> None:
        """Обновить объект Quote на месте."""
        r = self.locate(q.text)
        q.match = r["match"]
        q.score = r.get("score")
        if r["match"] in ("exact", "fuzzy"):
            q.text = r["text"]  # всегда подменяем на точный текст документа
            q.line_start, q.line_end, q.location = r["line_start"], r["line_end"], r["location"]
