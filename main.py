"""ClarifAI CLI: python main.py <файл требований> -o <файл отчёта> [--no-pdf] [--no-sim]"""
from __future__ import annotations

import argparse
import sys
import time


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):  # кириллица в консоли Windows
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="ClarifAI — ИИ-агент статической проверки требований (ISO/IEC/IEEE 29148, IEEE 830)")
    ap.add_argument("input", nargs="?", help="Путь к документу требований (.pdf, .docx, .md, .txt, .html)")
    ap.add_argument("-o", "--output", default="reports/report.md", help="Путь к отчёту (.md; рядом лягут .html и .pdf)")
    ap.add_argument("--no-pdf", action="store_true", help="Не формировать PDF")
    ap.add_argument("--no-html", action="store_true", help="Не формировать HTML")
    ap.add_argument("--no-sim", action="store_true", help="Отключить проверку расчётом (симуляцию)")
    ap.add_argument("--fast", action="store_true", help="Без дополнительных проходов (вопросы разработчика, сверка противоречий)")
    ap.add_argument("--no-critic", action="store_true", help="Отключить рецензента")
    ap.add_argument("--full", action="store_true", help="Полный отчёт (по умолчанию — краткий)")
    ap.add_argument("--lint", action="store_true", help="Добавить детерминированные проверки без ИИ (размытые слова, заглушки)")
    ap.add_argument("--no-cache", action="store_true", help="Не использовать кэш ответов LLM")
    ap.add_argument("--model", help="Переопределить модель (иначе из .env)")
    ap.add_argument("--list-models", action="store_true", help="Показать доступные модели и выйти")
    a = ap.parse_args(argv)

    from validator.config import Settings
    from validator.llm import LLM, LLMError, list_models
    from validator.parsing import load_document
    from validator.pipeline import Options, Pipeline
    from validator.report import write_reports

    s = Settings()
    if a.no_cache:
        s.use_cache = False
    if a.list_models:
        for m in list_models(s):
            print(m)
        return 0
    if not a.input:
        ap.error("укажите путь к документу требований")

    t0 = time.time()

    def progress(stage: str, msg: str) -> None:
        print(f"[{time.time() - t0:6.1f}s] {msg}", flush=True)

    try:
        doc = load_document(a.input)
        progress("load", f"Документ: «{doc.title}» ({doc.fmt}, {len(doc.lines)} строк)")
        llm = LLM(s, model=a.model)
        from validator.config import ENV_FILE

        progress("load", f"Модель: {llm.provider}/{llm.model} (настройки: {ENV_FILE if ENV_FILE.exists() else 'файл .env не найден!'})")
        res = Pipeline(llm=llm, settings=s, options=Options(simulate=not a.no_sim, critic=not a.no_critic, lint=a.lint, deep=not a.fast),
                       progress=progress).run(doc)
        files = write_reports(res, a.output, pdf=not a.no_pdf, html=not a.no_html, full=a.full)
    except (LLMError, FileNotFoundError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    act = res.active
    print(f"\nГотово за {time.time() - t0:.0f} с. Требований: {len(res.requirements)}, нарушений: {len(act)} "
          f"(критичных: {sum(f.severity == 'critical' for f in act)}). Итог: {res.verdict.ready}.")
    for k, p in files.items():
        print(f"  {k.upper():4} → {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
