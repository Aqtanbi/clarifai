"""Оценка полноты агента по эталону: python eval/run_eval.py reports/report.json [eval/gold.json]

Находка засчитывается эталонному дефекту, если её цитата содержит один из anchors,
а описание (проблема/рекомендация) — одно из keywords. Это грубая автоматическая
метрика для сравнения версий промптов/моделей, а не замена ручной проверке.
"""
import json
import sys
from pathlib import Path


def norm(s: str) -> str:
    return " ".join((s or "").lower().replace("ё", "е").replace("**", "").split())


def main():
    rep = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    gold = json.loads(Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).with_name("gold.json")).read_text(encoding="utf-8"))["items"]
    findings = [f for f in rep["findings"] if not f.get("dropped")]
    matched_f = set()
    rows = []
    for g in gold:
        anchors = [norm(a) for a in g["anchors"]]
        kws = [norm(k) for k in g["keywords"]]
        hits = []
        for f in findings:
            place = norm(" ".join(q["text"] for q in f["quotes"]))
            text = norm(" ".join([f["problem"], f.get("recommendation") or "", f.get("rewrite") or "", f.get("question") or ""]))
            if any(a in place for a in anchors) and any(k in text for k in kws):
                hits.append(f["id"])
        matched_f.update(hits)
        rows.append((g, hits))
    found = sum(1 for _, h in rows if h)
    crit = [r for r in rows if r[0]["severity"] == "critical"]
    print(f"Модель: {rep['passport']['model']}  ·  версия: {rep['passport']['agent_version']}")
    print(f"Полнота (recall): {found}/{len(gold)} = {found / len(gold):.0%}   ·   критичные: {sum(1 for _, h in crit if h)}/{len(crit)}")
    print(f"Находок всего: {len(findings)}, из них соответствуют эталону: {len(matched_f)} "
          f"(остальные — не обязательно ложные, проверьте вручную)\n")
    for g, hits in rows:
        print(f"{'✓' if hits else '✗'} {g['id']} [{g['severity']:8}] {g['title']}" + (f"  ← {', '.join(hits[:5])}" if hits else ""))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
