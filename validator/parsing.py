"""Чтение входного документа (PDF, DOCX, MD, TXT, HTML) в нумерованные строки с привязкой к странице и разделу."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED = (".pdf", ".docx", ".md", ".markdown", ".txt", ".html", ".htm")


@dataclass
class Line:
    no: int  # 1-based номер строки в нормализованном тексте
    text: str
    page: int | None
    section: str  # путь заголовков: "Часть A › Функциональные требования"


@dataclass
class Document:
    name: str
    title: str
    fmt: str
    sha256: str
    lines: list[Line] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)

    def numbered(self) -> str:
        """Текст для LLM: каждая непустая строка с номером L<n>."""
        return "\n".join(f"L{l.no}: {l.text}" for l in self.lines if l.text.strip())

    def line(self, no: int) -> Line | None:
        if 1 <= no <= len(self.lines):
            return self.lines[no - 1]
        return None

    def location(self, start: int, end: int | None = None) -> str:
        end = end or start
        l = self.line(start)
        rng = f"строка {start}" if start == end else f"строки {start}–{end}"
        parts = []
        if l and l.section:
            parts.append(l.section)
        if l and l.page:
            parts.append(f"с. {l.page}")
        return ", ".join(parts + [rng])


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _clean_md_heading(t: str) -> str:
    return re.sub(r"[*_`]+", "", t).strip()


def _build(name: str, fmt: str, raw: bytes, pages: list[tuple[int | None, str]]) -> Document:
    lines: list[Line] = []
    stack: list[tuple[int, str]] = []
    title = ""
    for page, text in pages:
        for rawline in text.splitlines():
            t = rawline.rstrip()
            m = _HEADING.match(t.strip())
            if m:
                level, head = len(m.group(1)), _clean_md_heading(m.group(2))
                if not title and level == 1:
                    title = head
                while stack and stack[-1][0] >= level:
                    stack.pop()
                if level > 1 or not stack:
                    stack.append((level, head))
            # Раздел для отображения: без заголовка 1-го уровня (это название документа)
            section = " › ".join(h for lv, h in stack if lv > 1 or len(stack) == 1)
            lines.append(Line(no=len(lines) + 1, text=t, page=page, section=section))
    # схлопываем серии пустых строк, сохраняя нумерацию последовательной
    compact: list[Line] = []
    for l in lines:
        if not l.text.strip() and compact and not compact[-1].text.strip():
            continue
        compact.append(Line(no=len(compact) + 1, text=l.text, page=l.page, section=l.section))
    if not title:
        first = next((l.text.strip() for l in compact if l.text.strip()), name)
        title = _clean_md_heading(first.lstrip("#").strip())[:150]
    return Document(name=name, title=title, fmt=fmt, sha256=hashlib.sha256(raw).hexdigest(), lines=compact)


def _area(r) -> float:
    return max(0.0, r.x1 - r.x0) * max(0.0, r.y1 - r.y0)


def _pdf_pages(raw: bytes) -> list[tuple[int | None, str]]:
    """PDF → markdown-подобный текст: заголовки по размеру/жирности шрифта, таблицы через find_tables()."""
    from collections import Counter

    import pymupdf  # PyMuPDF

    doc = pymupdf.open(stream=raw, filetype="pdf")
    # 1) типовой размер шрифта основного текста
    sizes: Counter = Counter()
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                for sp in l["spans"]:
                    if sp["text"].strip():
                        sizes[round(sp["size"], 1)] += len(sp["text"])
    body = sizes.most_common(1)[0][0] if sizes else 11.0
    head_sizes = sorted({sz for sz in sizes if sz >= body * 1.12}, reverse=True)[:5]

    def level(size: float, bold: bool) -> int:
        if size in head_sizes:
            return head_sizes.index(size) + 1
        if bold and size >= body * 0.98:
            return min(len(head_sizes) + 1, 6)
        return 0

    out: list[tuple[int | None, str]] = []
    for pno, page in enumerate(doc, 1):
        items: list[tuple[float, str]] = []  # (y, текст) — для сортировки по вертикали
        tables = []
        try:
            tables = list(page.find_tables().tables)
        except Exception:
            pass
        rects = [pymupdf.Rect(t.bbox) for t in tables]
        for t in tables:
            rows = t.extract()
            md = []
            for i, row in enumerate(rows):
                md.append("| " + " | ".join((c or "").replace("\n", " ").strip() for c in row) + " |")
                if i == 0:
                    md.append("|" + "---|" * len(row))
            items.append((t.bbox[1], "\n".join(md)))
        for b in page.get_text("dict", sort=True)["blocks"]:
            if b.get("type") != 0:
                continue
            bb = pymupdf.Rect(b["bbox"])
            if any(r.intersects(bb) and _area(r & bb) > 0.5 * _area(bb) for r in rects):
                continue
            for l in b.get("lines", []):
                spans = [sp for sp in l["spans"] if sp["text"].strip()]
                if not spans:
                    continue
                text = "".join(sp["text"] for sp in l["spans"]).strip()
                main = max(spans, key=lambda sp: len(sp["text"]))
                lv = level(round(main["size"], 1), bool(main["flags"] & 16) and all(sp["flags"] & 16 for sp in spans))
                if lv and len(text) < 150:
                    text = "#" * lv + " " + text
                items.append((l["bbox"][1], text))
        items.sort(key=lambda x: x[0])
        out.append((pno, "\n".join(t for _, t in items)))
    return out


def _docx_pages(raw: bytes) -> list[tuple[int | None, str]]:
    import io

    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(raw))
    body = list(d.element.body.iterchildren())

    def style_of(el) -> str:
        p = Paragraph(el, d)
        return (p.style.name or "").lower() if p.style is not None else ""

    # Если есть стиль «Title», он — уровень 1, а заголовки Heading N сдвигаются на уровень ниже
    has_title = any(el.tag.endswith("}p") and style_of(el).startswith(("title", "название")) for el in body)
    shift = 1 if has_title else 0
    out: list[str] = []
    counter = 0  # автонумерация Word не попадает в текст абзаца — восстанавливаем её
    for child in body:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            txt = p.text.strip()
            style = style_of(child)
            numbered = "list number" in style or "нумерованный" in style or child.find(".//" + qn("w:numPr")) is not None
            m = re.search(r"(heading|заголовок)\s*(\d)", style)
            if m and txt:
                out.append("#" * min(6, int(m.group(2)) + shift) + " " + txt)
                counter = 0
            elif style.startswith(("title", "название")) and txt:
                out.append("# " + txt)
            elif numbered and txt:
                counter += 1
                out.append(f"{counter}. {txt}")
            elif "list" in style and txt:
                out.append("* " + txt)
            else:
                if txt:
                    counter = 0
                out.append(txt)
        elif tag == "tbl":
            t = Table(child, d)
            for i, row in enumerate(t.rows):
                cells = [c.text.replace("\n", " ").strip() for c in row.cells]
                out.append("| " + " | ".join(cells) + " |")
                if i == 0:
                    out.append("|" + "---|" * len(cells))
            out.append("")
    return [(None, "\n".join(out))]


def _html_pages(raw: bytes) -> list[tuple[int | None, str]]:
    import html as h

    s = raw.decode("utf-8", errors="replace")
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    for lv in range(1, 7):
        s = re.sub(rf"(?is)<h{lv}[^>]*>(.*?)</h{lv}>", lambda m, lv=lv: "\n" + "#" * lv + " " + m.group(1) + "\n", s)
    s = re.sub(r"(?is)</(p|div|li|tr|br)\s*>|<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</t[dh]>", " | ", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    return [(None, h.unescape(s))]


def load_document(path: str | Path | None = None, *, data: bytes | None = None, filename: str | None = None) -> Document:
    """Загрузить документ с диска (path) или из байтов (data + filename)."""
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Файл не найден: {p}")
        data = p.read_bytes()
        filename = p.name
    if data is None or not filename:
        raise ValueError("Нужен path или data+filename")
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED:
        raise ValueError(f"Формат {ext} не поддерживается. Поддерживаются: {', '.join(SUPPORTED)}")
    if ext == ".pdf":
        pages = _pdf_pages(data)
    elif ext == ".docx":
        pages = _docx_pages(data)
    elif ext in (".html", ".htm"):
        pages = _html_pages(data)
    else:
        pages = [(None, data.decode("utf-8-sig", errors="replace"))]
    doc = _build(filename, ext.lstrip("."), data, pages)
    if not any(l.text.strip() for l in doc.lines):
        raise ValueError("Из документа не удалось извлечь текст (возможно, это скан без текстового слоя).")
    return doc
