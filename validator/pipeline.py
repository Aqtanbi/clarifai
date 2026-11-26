"""Конвейер проверки: нарезка → проверка требований → проверка набора → симуляция → линтер → grounding → критик."""
from __future__ import annotations

import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from .config import ROOT, Settings
from .criteria import INDIVIDUAL_CODES, SEVERITY_ORDER
from .dedupe import merge_duplicates
from .grounding import Grounder
from .lint import run_lint
from .llm import LLM
from .parsing import Document
from .prompts import (
    SYSTEM, consistency_prompt, critic_prompt, extract_prompt, probe_prompt, profile_context, profile_prompt, individual_prompt, set_prompt, simulation_fix_prompt, simulation_prompt,
)
from .sandbox import UnsafeCode, run_code
from .schemas import (
    Assessment, ConsistencyResponse, Coverage, CriticResponse, DocProfile, FindingsResponse, ProbeResponse, ExtractResponse, Finding, IndividualResponse, Quote, Requirement,
    SetResponse, SimOutput, SimulationCode, Verdict,
)

Progress = Callable[[str, str], None]


@dataclass
class Options:
    simulate: bool = True
    critic: bool = True
    deep: bool = True  # проходы «вопросы разработчика» и «сверка противоречий»
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
    consistency: ConsistencyResponse | None = None
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


def _same(a: str, b: str) -> bool:
    """Грубое сравнение результатов: без регистра, пробелов, знаков препинания и валюты."""
    norm = lambda s: re.sub(r"[\s.,;:«»\"'₸()]+", "", (s or "").lower().replace("ё", "е"))
    return norm(a) == norm(b)


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

    def individual_batch(self, doc: Document, tag: str, batch: list[Requirement]) -> tuple[dict[str, Assessment], list[Finding]]:
        resp = self.llm.ask(f"individual-{tag}", SYSTEM, individual_prompt(doc.numbered(), [_req_brief(r) for r in batch], self.ctx),
                            IndividualResponse, fix=_fix_findings)
        ids = {r.id for r in batch}
        assessments: dict[str, Assessment] = {}
        for a in resp.assessments:
            if a.req_id in ids and a.marks:
                a.marks = {k: v for k, v in a.marks.items() if k in INDIVIDUAL_CODES}
                assessments[a.req_id] = a
        findings = []
        for f in resp.findings:
            if f.req_ids and not ids.intersection(f.req_ids):
                continue  # находка не про требования этого пакета — её даст свой пакет
            f.scope, f.source = "requirement", "llm"
            findings.append(f)
        return assessments, findings

    @staticmethod
    def sync_marks(assessments: dict[str, Assessment], findings: list[Finding]) -> None:
        """Согласованность: fail в матрице ⇔ есть находка."""
        for f in findings:
            for rid in f.req_ids:
                a = assessments.setdefault(rid, Assessment(req_id=rid))
                if f.criterion in INDIVIDUAL_CODES:
                    a.marks[f.criterion] = "fail"

    @staticmethod
    def _known(findings: list[Finding]) -> list[str]:
        return [f"{','.join(f.req_ids) or 'набор'} {f.criterion}: {f.problem[:110]}" for f in findings]

    def consistency(self, doc: Document, reqs: list[Requirement], findings: list[Finding]) -> tuple[list[Finding], ConsistencyResponse]:
        self.progress("consistency", "Сверка противоречий: расчёт каждого критерия приёмки, пары правил")
        briefs = [_req_brief(r) for r in reqs]
        resp = self.llm.ask("consistency", SYSTEM, consistency_prompt(doc.numbered(), briefs, self._known(findings), self.ctx),
                            ConsistencyResponse, fix=_fix_findings)
        acc = [r.id for r in reqs if r.kind == "acceptance"]
        missing = [a for a in acc if a not in {c.ac_id for c in resp.ac_checks}]
        if missing:  # модель пропустила критерии — дозапрос только по ним
            self.progress("consistency", f"Нет расчёта для критериев {', '.join(missing)} — дозапрашиваю")
            extra = self.llm.ask("consistency-retry", SYSTEM, consistency_prompt(doc.numbered(), briefs, self._known(findings), self.ctx,
                                                                                 only_ac=missing), ConsistencyResponse, fix=_fix_findings)
            resp.ac_checks += [c for c in extra.ac_checks if c.ac_id in missing]
        ids = {r.id for r in reqs}
        out: list[Finding] = []
        for c in resp.ac_checks:
            if c.match is None:  # модель не заполнила вывод — сравниваем сами
                c.match = _same(c.computed, c.expected)
            if c.match:
                continue
            out.append(Finding(
                scope="set", source="llm", criterion="S-CON", severity="critical",
                req_ids=[x for x in [c.ac_id, *c.req_ids] if x in ids],
                quotes=[Quote(text=q) for q in (c.quote_ac, c.quote_rule) if q],
                problem=f"Критерий приёмки {c.ac_id} противоречит требованиям: ожидается «{c.expected}», а по тексту требований "
                        f"получается «{c.computed}». Расчёт: {c.steps}",
                recommendation="Привести требование и критерий приёмки к одному правилу и явно зафиксировать его.",
                question="Какой результат правильный — по требованию или по критерию приёмки?",
            ))
        for p in resp.pair_checks:
            if p.conflict is None:
                p.conflict = bool(p.result_a and p.result_b) and not _same(p.result_a, p.result_b)
            if not p.conflict:
                continue
            out.append(Finding(
                scope="set", source="llm", criterion="S-CON", severity="critical",
                req_ids=[x for x in (p.req_a, p.req_b) if x in ids],
                quotes=[Quote(text=q) for q in (p.quote_a, p.quote_b) if q],
                problem=f"Требования {p.req_a} и {p.req_b} противоречат друг другу в сценарии: {p.scenario} "
                        f"По {p.req_a}: {p.result_a}. По {p.req_b}: {p.result_b}.",
                recommendation="Определить приоритет правил или уточнить условия, при которых действует каждое.",
                question="Какое из правил должно действовать в этом сценарии?",
            ))
        for f in resp.findings:
            f.scope, f.source = "set", "llm"
            out.append(f)
        return out, resp

    def probe(self, doc: Document, reqs: list[Requirement], findings: list[Finding]) -> list[Finding]:
        targets = [r for r in reqs if r.kind != "acceptance"]  # все требования, кроме критериев приёмки
        if not targets:
            return []
        out: list[Finding] = []
        bs = max(1, self.s.batch_size)
        batches = [targets[i:i + bs] for i in range(0, len(targets), bs)]
        self.progress("probe", f"Вопросы разработчика: {len(targets)} требований, пакетов {len(batches)}")

        by_id = {r.id: r for r in reqs}

        def one(n_batch):
            n, batch = n_batch
            resp = self.llm.ask(f"probe-{n}", SYSTEM, probe_prompt(doc.numbered(), [_req_brief(r) for r in batch],
                                                                   self._known(findings), self.ctx), ProbeResponse)
            ids = {r.id for r in batch}
            groups: dict[tuple[str, str], list] = {}
            for p in resp.probes:
                if p.req_id not in ids or p.answered is not False:
                    continue
                crit = p.criterion if p.criterion in INDIVIDUAL_CODES else "CMP"
                groups.setdefault((p.req_id, crit), []).append(p)
            res = []
            for (rid, crit), ps in groups.items():  # вопросы одного требования и критерия — одна находка
                r = by_id[rid]
                sevs = [p.severity for p in ps if p.severity in SEVERITY_ORDER] or ["major"]
                qs = "; ".join(f"{i}) {p.question}" + (f" — {p.why}" if p.why else "") for i, p in enumerate(ps, 1))
                res.append(Finding(
                    scope="requirement", source="llm", criterion=crit, severity=min(sevs, key=SEVERITY_ORDER.get), req_ids=[rid],
                    quotes=[Quote(text=r.quote, line_start=r.line_start, line_end=r.line_end)],  # цитата — из проверенной нарезки
                    problem=f"Документ не отвечает на вопросы разработчика/тестировщика: {qs}",
                    recommendation="Дополнить требование ответами на эти вопросы.",
                    question=ps[0].question,
                ))
            return res

        with ThreadPoolExecutor(max_workers=max(1, self.s.parallel)) as ex:
            for res in ex.map(one, enumerate(batches, 1)):
                out += res
        return out

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
                    out = None
                    try:
                        out = SimOutput.model_validate(json.loads(rr.stdout.strip().splitlines()[-1] if rr.stdout.strip() else ""))
                    except Exception as e:  # stdout не JSON по контракту
                        try:
                            out = SimOutput.model_validate(json.loads(rr.stdout))
                        except Exception:
                            err = f"Вывод не соответствует контракту JSON: {e}\nstdout: {rr.stdout[:1500]}"
                    if out is not None:
                        res.update(status="ok", code=code, output=out)
                        # каждый критерий приёмки должен быть проверен своим сценарием
                        sources = " ".join(c.source for r_ in out.results for c in r_.checks)
                        missing = [r.id for r in reqs if r.kind == "acceptance" and r.id not in sources]
                        if not missing or attempt == 3:
                            return res
                        self.progress("simulate", f"Нет сценариев для критериев приёмки {', '.join(missing)} — дозапрашиваю")
                        err = (f"Программа выполнилась, но нарушено требование покрытия: нет сценариев для критериев приёмки "
                               f"{', '.join(missing)} (их id должны быть в поле source). Добавь по сценарию на каждый, сохранив существующие. "
                               f"Тексты: " + "; ".join(f"{r.id}: {r.quote}" for r in reqs if r.id in missing))
            except UnsafeCode as e:
                err = f"Код отклонён проверкой безопасности: {e}"
            res["errors"].append(err)
            if attempt == 3:
                break
            fixed = self.llm.ask(f"simulate-fix-{attempt}", SYSTEM, simulation_fix_prompt(code, err), SimulationCode)
            code = fixed.code or code
        if res.get("status") != "ok":
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
                rids = list(dict.fromkeys(re.findall(r"R-\d+", c.source)
                                          + [rid for key, rid in by_doc_id.items() if key and key in c.source]))
                rids = [x for x in rids if x in {r.id for r in reqs}]
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
        drops = [d for d in resp.decisions if d.action == "drop" and d.id in by_id]
        dropped_ids = {d.id for d in drops if d.reason_code in ("duplicate", "wrong")}
        for d in resp.decisions:
            f = by_id.get(d.id)
            if not f or f.source == "rule":
                continue
            if d.action == "drop":
                code = (d.reason_code or "").lower()
                if code == "duplicate" and d.duplicate_of in by_id and d.duplicate_of not in dropped_ids:
                    f.dropped, f.drop_reason = True, f"Рецензент: дубликат {d.duplicate_of}. {d.reason}".strip()
                elif code == "wrong" and f.source != "simulation":
                    f.dropped, f.drop_reason = True, f"Рецензент: не подтверждается документом. {d.reason}".strip()
                else:
                    # «общая фраза» / «вкусовщина» / без обоснования — не удаляем, а понижаем: пропуск дефекта дороже
                    f.severity = "minor"
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
        # ---- Фаза 2: независимые проверки параллельно ----
        bs = max(1, self.s.batch_size)
        batches = [reqs[i:i + bs] for i in range(0, len(reqs), bs)]
        sim_skip = None
        if not self.o.simulate:
            sim_skip = {"status": "disabled", "reason": "Отключено параметром запуска", "code": "", "output": None, "attempts": 0, "errors": []}
        elif prof is not None and not prof.formalizable:
            sim_skip = {"status": "skipped", "reason": "По профилю документа в нём нет правил с числами/условиями и примеров, которые можно проверить расчётом",
                        "code": "", "output": None, "attempts": 0, "errors": []}
        st = time.time()
        self.progress("parallel", f"Параллельно: проверка {len(reqs)} требований ({len(batches)} пакетов), набора"
                      + (", сверка противоречий" if self.o.deep else "") + (", проверка расчётом" if not sim_skip else ""))
        with ThreadPoolExecutor(max_workers=max(1, self.s.parallel)) as ex:
            fut_ind = [ex.submit(self.individual_batch, doc, str(n), b) for n, b in enumerate(batches, 1)]
            fut_set = ex.submit(self.check_set, doc, reqs)
            fut_con = ex.submit(self.consistency, doc, reqs, []) if self.o.deep else None
            fut_sim = ex.submit(self.simulate, doc, reqs) if not sim_skip else None
            assessments: dict[str, Assessment] = {}
            findings: list[Finding] = []
            for fu in fut_ind:
                a, fs = fu.result()
                assessments.update(a)
                findings += fs
            set_resp = fut_set.result()
            findings += set_resp.findings
            cons = None
            if fut_con:
                try:
                    cf, cons = fut_con.result()
                    findings += cf
                except Exception as e:  # дополнительный проход не должен ронять прогон
                    self.progress("consistency", f"Сверка противоречий пропущена ({str(e)[:150]})")
            if fut_sim:
                try:
                    sim = fut_sim.result()
                except Exception as e:  # симуляция не должна ронять весь прогон
                    sim = {"status": "failed", "reason": str(e)[:500], "code": "", "output": None, "attempts": 0, "errors": [str(e)]}
            else:
                sim = sim_skip
        findings += self.simulation_findings(sim, reqs)
        tick("Проверки (параллельно)", st)

        # ТЗ: проверка КАЖДОГО требования — дозапрос оценки пропущенных моделью
        missing = [r for r in reqs if r.id not in assessments]
        if missing:
            self.progress("individual", f"Модель пропустила оценку {len(missing)} требований — дозапрашиваю")
            for i in range(0, len(missing), bs):
                a, fs = self.individual_batch(doc, f"retry-{i // bs + 1}", missing[i:i + bs])
                assessments.update(a)
                findings += fs
        still = [r.id for r in reqs if r.id not in assessments]
        if still:
            self.progress("individual", f"Без оценки модели остались: {', '.join(still)}")

        # ---- Фаза 3: вопросы разработчика (учитывает уже найденное) ----
        if self.o.deep:
            st = time.time()
            try:
                findings += self.probe(doc, reqs, findings)
            except Exception as e:
                self.progress("probe", f"Вопросы разработчика: этап пропущен ({str(e)[:150]})")
            tick("Вопросы разработчика", st)
        self.sync_marks(assessments, findings)

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
        merged = merge_duplicates(findings, reqs)
        if merged:
            self.progress("dedupe", f"Склеено дублей: {merged}")

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
            "model": (", ".join(f"{m} — {n} запр." for m, n in self.llm.stats.models_used.items())
                      if set(self.llm.stats.models_used) - {self.llm.model} else self.llm.model),
            "agent_version": agent_version(),
            "llm_calls": self.llm.stats.calls,
            "llm_cached": self.llm.stats.cached,
            "tokens": f"{self.llm.stats.input_tokens} вход / {self.llm.stats.output_tokens} выход",
            "duration": f"{time.time() - t0:.0f} с",
            "lines": len(doc.lines),
        }
        return RunResult(doc=doc, requirements=reqs, assessments=assessments, findings=findings,
                         coverage=set_resp.coverage, glossary_gaps=set_resp.glossary_gaps, verdict=verdict,
                         simulation=sim, passport=passport, grounding=dict(g.stats), timings=timings, profile=prof,
                         consistency=cons)


def run_file(path: str | Path, options: Options | None = None, progress: Progress | None = None, llm: LLM | None = None) -> RunResult:
    from .parsing import load_document

    doc = load_document(path)
    return Pipeline(llm=llm, options=options, progress=progress).run(doc)
