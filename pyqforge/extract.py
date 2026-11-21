"""Per-page extraction: detect -> annotate -> LLM -> validate -> (optional) cross-verify.

Results are cached as work/<book>/pages/pNNNN.json so runs are resumable: if a
subscription window runs out mid-book, re-run the same command later and only the
missing pages are processed.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .engines import AllEnginesExhausted, EnginePool, EngineResult, Usage
from .ingest import LLM_LONG_EDGE, PageInfo
from .layout import Candidate, annotate_for_llm, detect_figures
from .schema import PAGE_SCHEMA, PageResult

log = logging.getLogger("pyqforge")
PROMPTS = Path(__file__).parent / "prompts"
CACHE_VERSION = 1


def _render(template: str, **kw: str) -> str:
    for k, v in kw.items():
        template = template.replace("{" + k + "}", v)
    return template


def build_prompt(page: PageInfo, candidates: list[Candidate]) -> str:
    labels = ", ".join(c.label for c in candidates) or "none (no boxes drawn)"
    hint = ""
    if page.text_hint:
        hint = ("## Text layer\nThis PDF page has a text layer. Use it to get spellings and numbers right, "
                "but trust the image for layout, math and figure placement:\n```\n"
                + page.text_hint[:6000] + "\n```\n\n")
    return _render((PROMPTS / "page_extract.md").read_text(), page_number=str(page.number),
                   candidate_labels=labels, text_hint_block=hint)


def page_needs_review(res: PageResult, min_conf: float) -> list[str]:
    reasons = []
    for it in res.items:
        tag = it.qid or "?"
        if it.confidence < min_conf:
            reasons.append(f"{tag}: low confidence {it.confidence:.2f}")
        if it.missing_figures:
            reasons.append(f"{tag}: model reported unboxed figure")
        if it.kind == "question" and it.question_type in ("mcq", "msq") and len(it.options) not in (4, 5):
            reasons.append(f"{tag}: {len(it.options)} options")
        if it.kind == "question" and not it.qid and not it.is_continuation:
            reasons.append("question without qid")
    if res.page_type in ("questions", "solutions", "mixed") and not res.items and not res.answer_key:
        reasons.append("content page with no items")
    return reasons


@dataclass
class PageOutcome:
    page: PageInfo
    candidates: list[Candidate]
    result: PageResult | None
    engine: str | None
    model: str | None
    verified_by: str | None
    review_reasons: list[str]
    usage: Usage
    error: str | None = None


class PageExtractor:
    def __init__(self, pool: EnginePool, work_dir: Path, verify: bool = False,
                 min_conf: float = 0.75, prefer: str | None = None, force: bool = False,
                 verify_same: bool = False):
        self.pool = pool
        self.work_dir = work_dir
        self.verify = verify
        self.verify_same = verify_same
        self.min_conf = min_conf
        self.prefer = prefer
        self.force = force
        self.system = (PROMPTS / "system.md").read_text().strip()
        (work_dir / "pages").mkdir(parents=True, exist_ok=True)
        (work_dir / "llm_images").mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- cache
    def _cache_path(self, page: PageInfo) -> Path:
        return self.work_dir / "pages" / f"p{page.number:04d}.json"

    def _load_cached(self, page: PageInfo) -> PageOutcome | None:
        p = self._cache_path(page)
        if self.force or not p.exists():
            return None
        d = json.loads(p.read_text())
        if d.get("cache_version") != CACHE_VERSION or d.get("result") is None:
            return None
        return PageOutcome(
            page=page,
            candidates=[Candidate(c["label"], tuple(c["box"])) for c in d["candidates"]],
            result=PageResult.model_validate(d["result"]), engine=d.get("engine"),
            model=d.get("model"), verified_by=d.get("verified_by"),
            review_reasons=d.get("review_reasons", []), usage=Usage(),
        )

    def _save(self, o: PageOutcome) -> None:
        self._cache_path(o.page).write_text(json.dumps({
            "cache_version": CACHE_VERSION, "page": o.page.number, "is_digital": o.page.is_digital,
            "candidates": [{"label": c.label, "box": list(c.box)} for c in o.candidates],
            "result": o.result.model_dump() if o.result else None,
            "engine": o.engine, "model": o.model, "verified_by": o.verified_by,
            "review_reasons": o.review_reasons, "usage": o.usage.as_dict(), "error": o.error,
        }, indent=2, ensure_ascii=False))

    # -------------------------------------------------------------- core
    def _call(self, prompt: str, image: Path, prefer: str | None) -> tuple[PageResult, EngineResult]:
        er = self.pool.run(self.system, prompt, image, PAGE_SCHEMA, prefer=prefer)
        try:
            return PageResult.model_validate(er.data), er
        except ValidationError:
            # one corrective retry on the same engine
            er = self.pool.run(self.system, prompt + "\n\nYour previous answer did not match the schema. "
                               "Return valid JSON only.", image, PAGE_SCHEMA, prefer=er.engine)
            return PageResult.model_validate(er.data), er

    def process(self, page: PageInfo) -> PageOutcome:
        cached = self._load_cached(page)
        if cached:
            return cached
        usage = Usage()
        cands = detect_figures(page.render_path, page.digital_boxes)
        img = annotate_for_llm(page.render_path, cands,
                               self.work_dir / "llm_images" / f"p{page.number:04d}.jpg", LLM_LONG_EDGE)
        prompt = build_prompt(page, cands)
        try:
            res, er = self._call(prompt, img, self.prefer)
        except (AllEnginesExhausted, ValidationError) as e:
            o = PageOutcome(page, cands, None, None, None, None, [], usage, error=str(e))
            self._save(o)
            return o
        usage.add(er.usage)
        reasons = page_needs_review(res, self.min_conf)
        verified_by = None
        if self.verify and reasons:
            other = None if self.verify_same else self.pool.other_than(er.engine)
            if other is None:  # single-engine run (or the other one is parked): self-review pass
                other = next((e for e in self.pool.alive() if e.name == er.engine), None)
            if other is not None:
                vprompt = _render((PROMPTS / "verify.md").read_text(), page_number=str(page.number),
                                  previous_json=json.dumps(res.model_dump(), ensure_ascii=False, indent=1),
                                  base_prompt=prompt)
                try:
                    vres, ver = self._call(vprompt, img, other.name)
                    usage.add(ver.usage)
                    res, verified_by = vres, ver.engine
                    reasons = page_needs_review(res, self.min_conf)
                except (AllEnginesExhausted, ValidationError) as e:
                    log.warning("page %s: verification failed: %s", page.number, e)
        o = PageOutcome(page, cands, res, er.engine, er.model, verified_by, reasons, usage)
        self._save(o)
        return o

    def process_all(self, pages: list[PageInfo], workers: int = 3, on_done=None) -> list[PageOutcome]:
        outcomes: dict[int, PageOutcome] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(self.process, p): p for p in pages}
            for f in as_completed(futs):
                p = futs[f]
                try:
                    o = f.result()
                except Exception as e:  # never lose the whole run for one page
                    log.exception("page %s crashed", p.number)
                    o = PageOutcome(p, [], None, None, None, None, [], Usage(), error=repr(e))
                outcomes[p.number] = o
                if on_done:
                    on_done(o, len(outcomes), len(pages))
        return [outcomes[p.number] for p in pages]
