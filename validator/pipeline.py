"""Конвейер проверки: нарезка → проверка требований → проверка набора → симуляция → линтер → grounding → критик."""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from .config import ROOT, Settings
from .criteria import INDIVIDUAL_CODES, SEVERITY_ORDER
from .grounding import Grounder
from .lint import run_lint
from .llm import LLM
from .parsing import Document
from .prompts import (
    SYSTEM, critic_prompt, extract_prompt, profile_context, profile_prompt, individual_prompt, set_prompt, simulation_fix_prompt, simulation_prompt,
)
from .sandbox import UnsafeCode, run_code
from .schemas import (
    Assessment, Coverage, CriticResponse, DocProfile, ExtractResponse, Finding, IndividualResponse, Quote, Requirement,
    SetResponse, SimOutput, SimulationCode, Verdict,
)

Progress = Callable[[str, str], None]


@dataclass
class Options:
    simulate: bool = True
    critic: bool = True
    lint: bool = False  # детерминированный линтер выключен: все проверки делает ИИ


@dataclass
class RunResult:
    doc: Document
    requirements: list[Requirement]
    assessments: dict[str, Assessment]
    findings: list[Finding]  # включая отброшенные (dropped=True)
    coverage: list[Coverage]
    glossary_gaps: list[str]
    verdict: Verdict
    simulation: dict  # {"status", "reason", "code", "output": SimOutput|None, "attempts"}
    passport: dict
    grounding: dict
    profile: DocProfile | None = None
    timings: dict = field(default_factory=dict)

    @property
    def active(self) -> list[Finding]:
        return [f for f in self.findings if not f.dropped]


def agent_version() -> str:
    import os

    sha = os.getenv("VERCEL_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT")
    if sha:
        return f"ClarifAI v{__version__} ({sha[:8]})"
    try:
        desc = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip()
        if desc:
            return f"ClarifAI v{__version__} (git {desc})"
    except Exception:
        pass
    return f"ClarifAI v{__version__} (git недоступен)"


def _req_brief(r: Requirement) -> dict:
    return {"id": r.id, "doc_id": r.doc_id, "kind": r.kind, "section": r.section, "quote": r.quote,
            "lines": f"L{r.line_start}-L{r.line_end}" if r.line_start else None}


def _fix_findings(data):
    """Мягко нормализуем ответ модели: quotes строкой → объект, req_id → req_ids."""
    if isinstance(data, dict):
        for f in data.get("findings", []) or []:
            if isinstance(f, dict):
                if "req_id" in f and "req_ids" not in f:
                    f["req_ids"] = [f.pop("req_id")]
                qs = f.get("quotes") or ([f.pop("quote")] if f.get("quote") else [])
                f["quotes"] = [q if isinstance(q, dict) else {"text": str(q)} for q in qs]
    return data


class Pipeline:
    def __init__(self, llm: LLM | None = None, settings: Settings | None = None, options: Options | None = None,
                 progress: Progress | None = None):
        self.s = settings or Settings()
        self.llm = llm or LLM(self.s)
        self.o = options or Options()
        self.progress = progress or (lambda stage, msg: None)
        if hasattr(self.llm, "log"):
            self.llm.log = lambda msg: self.progress("llm", msg)

    ctx: str = ""

    # ---------- этапы ----------
    def profile(self, doc: Document) -> DocProfile | None:
        self.progress("profile", "Изучаю документ: тип, структура, риски предметной области")
        try:
            p = self.llm.ask("profile", SYSTEM, profile_prompt(doc.numbered()), DocProfile)
        except Exception as e:  # профиль улучшает качество, но не обязателен
            self.progress("profile", f"Профиль не построен ({str(e)[:120]}), продолжаю без него")
            return None
        self.ctx = profile_context(p)
        self.progress("profile", f"Документ: {p.doc_type}, {p.domain}; проверок под документ: {len(p.focus_checks)}")
        return p

    def extract(self, doc: Document, g: Grounder) -> list[Requirement]:
        self.progress("extract", "Нарезка требований")
        resp = self.llm.ask("extract", SYSTEM, extract_prompt(doc.numbered(), self.ctx), ExtractResponse)
        reqs: list[Requirement] = []
        for i, r in enumerate(resp.requirements, 1):
            r.id = f"R-{i:02d}"  # нормализуем нумерацию
            q = Quote(text=r.quote, line_start=r.line_start, line_end=r.line_end)
            g.ground_quote(q)
            r.quote, r.quote_match = q.text, q.match
            if q.line_start:
                r.line_start, r.line_end = q.line_start, q.line_end
            r.location = doc.location(r.line_start, r.line_end) if r.line_start else None
            reqs.append(r)
        self.progress("extract", f"Выделено требований: {len(reqs)}")
        return reqs

    def check_individual(self, doc: Document, reqs: list[Requirement]) -> tuple[dict[str, Assessment], list[Finding]]:
        assessments: dict[str, Assessment] = {}
        findings: list[Finding] = []
        bs = max(1, self.s.batch_size)
        batches = [reqs[i:i + bs] for i in range(0, len(reqs), bs)]
        for n, batch in enumerate(batches, 1):
            self.progress("individual", f"Проверка требований: пакет {n}/{len(batches)}")
            resp = self.llm.ask(f"individual-{n}", SYSTEM, individual_prompt(doc.numbered(), [_req_brief(r) for r in batch], self.ctx),
                                IndividualResponse, fix=_fix_findings)
            ids = {r.id for r in batch}
            for a in resp.assessments:
                if a.req_id in ids:
                    a.marks = {k: v for k, v in a.marks.items() if k in INDIVIDUAL_CODES}
                    assessments[a.req_id] = a
            for f in resp.findings:
                f.scope, f.source = "requirement", "llm"
                findings.append(f)
        # гарантируем согласованность: fail в матрице ⇔ есть находка
        for f in findings:
            for rid in f.req_ids:
                a = assessments.setdefault(rid, Assessment(req_id=rid))
                if f.criterion in INDIVIDUAL_CODES:
                    a.marks[f.criterion] = "fail"
        return assessments, findings

    def check_set(self, doc: Document, reqs: list[Requirement]) -> SetResponse:
        self.progress("set", "Проверка набора требований целиком")
        resp = self.llm.ask("set", SYSTEM, set_prompt(doc.numbered(), [_req_brief(r) for r in reqs], self.ctx), SetResponse,
                            fix=_fix_findings)
        for f in resp.findings:
            f.scope, f.source = "set", "llm"
        return resp

    def simulate(self, doc: Document, reqs: list[Requirement]) -> dict:
        self.progress("simulate", "Строю исполняемую модель требований")
        res: dict = {"status": "skipped", "reason": "", "code": "", "output": None, "attempts": 0, "errors": []}
        sc = self.llm.ask("simulate", SYSTEM, simulation_prompt(doc.numbered(), [_req_brief(r) for r in reqs], self.ctx), SimulationCode)
        if not sc.applicable or not sc.code.strip():
            res["reason"] = sc.reason or "Нет формализуемых правил"
            return res
        code = sc.code
        res["reason"] = sc.reason
        for attempt in range(1, 4):
            res["attempts"] = attempt
            self.progress("simulate", f"Прогон сценариев (попытка {attempt})")
            try:
                rr = run_code(code, timeout=self.s.sim_timeout)
                err = None if rr.ok else (rr.stderr or f"код возврата {rr.returncode}")
                if not err:
                    try:
                        out = SimOutput.model_validate(json.loads(rr.stdout.strip().splitlines()[-1] if rr.stdout.strip() else ""))
                        res.update(status="ok", code=code, output=out)
                        return res
                    except Exception as e:  # stdout не JSON по контракту
                        try:
                            out = SimOutput.model_validate(json.loads(rr.stdout))
                            res.update(status="ok", code=code, output=out)
                            return res
                        except Exception:
                            err = f"Вывод не соответствует контракту JSON: {e}\nstdout: {rr.stdout[:1500]}"
            except UnsafeCode as e:
                err = f"Код отклонён проверкой безопасности: {e}"
            res["errors"].append(err)
            if attempt == 3:
                break
            fixed = self.llm.ask(f"simulate-fix-{attempt}", SYSTEM, simulation_fix_prompt(code, err), SimulationCode)
            code = fixed.code or code
        res.update(status="failed", code=code)
        return res

    @staticmethod
    def simulation_findings(sim: dict, reqs: list[Requirement]) -> list[Finding]:
        out: SimOutput | None = sim.get("output")
        if not out:
            return []
        literal = {v.name for v in out.variants if v.literal} or {"literal"}
        by_doc_id = {(r.doc_id or "").strip().rstrip("."): r.id for r in reqs if r.doc_id}
        findings = []
        for res in out.results:
            if res.variant not in literal:
                continue
            for c in res.checks:
                if c.ok:
                    continue
                # варианты, при которых сценарий проходит — признак неоднозначности
                passing = [r.variant for r in out.results
                           if r.scenario_id == res.scenario_id and r.variant not in literal and all(x.ok for x in r.checks)]
                rids = [rid for key, rid in by_doc_id.items() if key and key in c.source]
                ev = f"Сценарий {res.scenario_id} «{res.title}»: по источнику {c.source} ожидается «{c.expected}», по модели остальных правил получается «{c.actual}»."
                if passing:
                    ev += f" Сценарий проходит только при альтернативном прочтении: {', '.join(passing)}."
                findings.append(Finding(
                    scope="simulation", source="simulation", criterion="S-CON" if not passing else "UNA",
                    severity="critical", req_ids=rids,
                    quotes=[Quote(text=c.quote)] if c.quote else [],
                    problem=("Противоречие подтверждено расчётом. " if not passing else "Неоднозначность подтверждена расчётом. ") + ev,
                    recommendation="Привести требование и критерий приёмки/смежное требование к одному правилу; зафиксировать выбранное прочтение явно.",
                    evidence=ev,
                ))
        return findings

    def critic(self, doc: Document, reqs: list[Requirement], findings: list[Finding]) -> Verdict:
        self.progress("critic", "Рецензент проверяет находки")
        brief = [{"id": f.id, "scope": f.scope, "source": f.source, "req_ids": f.req_ids, "criterion": f.criterion,
                  "severity": f.severity, "quotes": [q.text for q in f.quotes], "problem": f.problem} for f in findings]
        resp = self.llm.ask("critic", SYSTEM, critic_prompt(doc.numbered(), [_req_brief(r) for r in reqs], brief), CriticResponse)
        by_id = {f.id: f for f in findings}
        for d in resp.decisions:
            f = by_id.get(d.id)
            if not f:
                continue
            if d.action == "drop" and f.source != "rule":
                f.dropped, f.drop_reason = True, f"Рецензент: {d.reason}"
            elif d.severity:
                f.severity = d.severity
        return resp.verdict

    # ---------- запуск ----------
    def run(self, doc: Document) -> RunResult:
        t0 = time.time()
        timings: dict[str, float] = {}

        def tick(name: str, start: float):
            timings[name] = round(time.time() - start, 1)

        g = Grounder(doc)
        st = time.time(); prof = self.profile(doc); tick("Профиль", st)
        st = time.time(); reqs = self.extract(doc, g); tick("Нарезка", st)
        st = time.time(); assessments, findings = self.check_individual(doc, reqs); tick("Проверка требований", st)
        st = time.time(); set_resp = self.check_set(doc, reqs); tick("Проверка набора", st)
        findings += set_resp.findings

        sim = {"status": "disabled", "reason": "Отключено параметром запуска", "code": "", "output": None, "attempts": 0, "errors": []}
        if self.o.simulate and prof is not None and not prof.formalizable:
            sim = {"status": "skipped", "reason": "По профилю документа в нём нет правил с числами/условиями и примеров, которые можно проверить расчётом",
                   "code": "", "output": None, "attempts": 0, "errors": []}
        elif self.o.simulate:
            st = time.time()
            try:
                sim = self.simulate(doc, reqs)
            except Exception as e:  # симуляция не должна ронять весь прогон
                sim = {"status": "failed", "reason": str(e)[:500], "code": "", "output": None, "attempts": 0, "errors": [str(e)]}
            findings += self.simulation_findings(sim, reqs)
            tick("Симуляция", st)

        if self.o.lint:
            findings += run_lint(doc, reqs)

        # grounding всех цитат
        self.progress("grounding", "Проверяю цитаты по тексту документа")
        valid_ids = {r.id for r in reqs}
        for f in findings:
            f.req_ids = [r for r in f.req_ids if r in valid_ids]
            for q in f.quotes:
                g.ground_quote(q)
            if f.quotes and all(q.match == "not_found" for q in f.quotes):
                f.grounded = False
                f.dropped, f.drop_reason = True, "Grounding: ни одна цитата не найдена в документе"
            elif not f.quotes:
                f.grounded = False
                f.dropped, f.drop_reason = True, "Нет ссылки на текст документа"
            else:
                f.quotes = [q for q in f.quotes if q.match != "not_found"]
            if not f.req_ids:  # привязка к требованию по строкам цитаты
                for q in f.quotes:
                    for r in reqs:
                        if q.line_start and r.line_start and r.line_start <= q.line_start <= (r.line_end or r.line_start):
                            if r.id not in f.req_ids:
                                f.req_ids.append(r.id)

        # сортировка и нумерация
        findings.sort(key=lambda f: (f.dropped, SEVERITY_ORDER[f.severity], {"simulation": 0, "set": 1, "requirement": 2, "lint": 3}[f.scope]))
        for i, f in enumerate(findings, 1):
            f.id = f"F-{i:03d}"

        verdict = None
        if self.o.critic:
            st = time.time()
            try:
                verdict = self.critic(doc, reqs, [f for f in findings if not f.dropped])
            except Exception as e:
                self.progress("critic", f"Рецензент недоступен: {e}")
            tick("Рецензент", st)
        active = [f for f in findings if not f.dropped]
        crit = [f for f in active if f.severity == "critical"]
        if verdict is None:
            verdict = Verdict(ready="нет" if crit else ("условно" if active else "да"),
                              summary="Итог сформирован по правилам без рецензента: " + (f"{len(crit)} критичных дефектов." if crit else "критичных дефектов нет."),
                              top_fixes=[])
        # защитное правило: при критичных дефектах «да» невозможно
        if crit and verdict.ready == "да":
            verdict.ready = "условно"
        # повторная сортировка после правки серьёзности рецензентом (id сохраняем)
        findings.sort(key=lambda f: (f.dropped, SEVERITY_ORDER[f.severity], f.id))

        passport = {
            "document": doc.title,
            "file": doc.name,
            "format": doc.fmt.upper(),
            "sha256": doc.sha256[:16],
            "date": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)"),
            "provider": self.llm.provider,
            "model": self.llm.model,
            "agent_version": agent_version(),
            "llm_calls": self.llm.stats.calls,
            "llm_cached": self.llm.stats.cached,
            "tokens": f"{self.llm.stats.input_tokens} вход / {self.llm.stats.output_tokens} выход",
            "duration": f"{time.time() - t0:.0f} с",
            "lines": len(doc.lines),
        }
        return RunResult(doc=doc, requirements=reqs, assessments=assessments, findings=findings,
                         coverage=set_resp.coverage, glossary_gaps=set_resp.glossary_gaps, verdict=verdict,
                         simulation=sim, passport=passport, grounding=dict(g.stats), timings=timings, profile=prof)


def run_file(path: str | Path, options: Options | None = None, progress: Progress | None = None, llm: LLM | None = None) -> RunResult:
    from .parsing import load_document

    doc = load_document(path)
    return Pipeline(llm=llm, options=options, progress=progress).run(doc)
