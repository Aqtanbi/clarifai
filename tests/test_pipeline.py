"""Офлайн-тест конвейера на моке LLM: python -m pytest -q  (или python tests/test_pipeline.py)"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mock_llm import MockLLM  # noqa: E402

from validator.parsing import load_document  # noqa: E402
from validator.pipeline import Pipeline  # noqa: E402
from validator.report import write_reports  # noqa: E402

SAMPLE = ROOT / "tests" / "fixtures" / "sample.md"


def test_pipeline(tmp_path=None):
    out = Path(tmp_path or ROOT / "reports") / "mock_report.md"
    doc = load_document(SAMPLE)
    res = Pipeline(llm=MockLLM()).run(doc)
    files = write_reports(res, out)
    assert files["pdf"].stat().st_size > 5000
    # галлюцинация отброшена grounding-ом
    assert any(f.dropped and "Grounding" in (f.drop_reason or "") for f in res.findings)
    # симуляция нашла противоречие
    assert any(f.scope == "simulation" for f in res.active)
    assert res.verdict.ready == "нет"
    print("OK", {k: str(v) for k, v in files.items()})


if __name__ == "__main__":
    test_pipeline()
