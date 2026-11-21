"""Book-level assembly: continuations, figure crops, question<->solution linking, flags."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .extract import PageOutcome
from .layout import crop, refine_box
from .schema import Item

FIG_MENTION = re.compile(r"(\bfigure\b|\bfig\.|\bas shown\b|\bshown (below|above|in)\b|\bgiven below\b.*\b(beam|frame|truss|circuit)\b)", re.I)


def norm_qid(q: str | None) -> str | None:
    if not q:
        return None
    q = q.strip().lower()
    q = re.sub(r"^(q\.?|que\.?|question)\s*", "", q)
    q = re.sub(r"\s+", "", q).rstrip(".")
    return q or None


def norm_answer(a: str | None) -> str:
    """'(b)' -> 'b', 'a, c' -> 'a,c', '18 N' -> '18', '197.0' -> '197', '30 to 31' -> '30-31'."""
    if not a:
        return ""
    a = a.strip().lower()
    nums = re.findall(r"-?\d+(?:\.\d+)?", a)
    if nums:
        return "-".join(str(float(n)).rstrip("0").rstrip(".") for n in nums)
    letters = re.findall(r"\b[a-e]\b", a.replace("(", " ").replace(")", " "))
    return ",".join(sorted(set(letters))) or re.sub(r"\W+", "", a)


@dataclass
class Rec:
    kind: str
    qid: str | None
    page: int
    subject: str | None
    chapter: str | None
    item: Item
    text: str
    options: list[dict]
    figures: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)


class FigureStore:
    def __init__(self, out_dir: Path, static_prefix: str):
        self.dir = out_dir / "figures"
        self.prefix = static_prefix.rstrip("/")
        self._done: dict[tuple[int, str], str] = {}

    def from_label(self, o: PageOutcome, label: str) -> str | None:
        key = (o.page.number, label)
        if key in self._done:
            return self._done[key]
        box = next((c.box for c in o.candidates if c.label == label.strip().upper()), None)
        if box is None:
            return None
        name = f"p{o.page.number:04d}_{label.upper()}.png"
        crop(o.page.render_path, box, self.dir / name)
        self._done[key] = f"{self.prefix}/figures/{name}"
        return self._done[key]

    def from_norm_box(self, o: PageOutcome, idx: int, mf) -> str | None:
        key = (o.page.number, f"M{idx}")
        if key in self._done:
            return self._done[key]
        box = refine_box(o.page.render_path, (mf.x0, mf.y0, mf.x1, mf.y1))
        if box is None:
            return None
        name = f"p{o.page.number:04d}_M{idx}.png"
        crop(o.page.render_path, box, self.dir / name)
        self._done[key] = f"{self.prefix}/figures/{name}"
        return self._done[key]


def _flags_for(q: dict) -> list[str]:
    flags = []
    if q["solution"] is None:
        flags.append("no_solution")
    has_fig = bool(q["figures"]) or any(op["figures"] for op in q["options"])
    if FIG_MENTION.search(q["text"]) and not has_fig:
        flags.append("figure_mentioned_but_missing")
    if q["question_type"] in ("mcq", "msq") and len(q["options"]) not in (4, 5):
        flags.append(f"option_count_{len(q['options'])}")
    ans = (q["solution"] or {}).get("answer") or q.get("answer_key")
    if ans and q["options"] and q["question_type"] in ("mcq", "msq"):
        labels = {op["label"].lower() for op in q["options"]}
        given = {a.strip().lower().strip("()") for a in re.split(r"[,&/ ]+", ans) if a.strip()}
        if given and not given <= labels and "*" not in ans:
            flags.append("answer_not_in_options")
    if q["solution"] and q.get("answer_key") and q["solution"].get("answer"):
        if norm_answer(q["answer_key"]) != norm_answer(q["solution"]["answer"]):
            flags.append("answer_key_mismatch")
    if q["confidence"] < 0.75:
        flags.append("low_confidence")
    return flags


def assemble(outcomes: list[PageOutcome], out_dir: Path, static_prefix: str) -> dict:
    figs = FigureStore(out_dir, static_prefix)
    recs: list[Rec] = []
    key_entries: list[tuple[int, str, str]] = []
    page_errors, review = [], []
    subject = chapter = ch_num = ch_title = None
    mcount: dict[int, int] = {}

    for o in sorted(outcomes, key=lambda x: x.page.number):
        if o.result is None:
            page_errors.append({"page": o.page.number, "error": o.error})
            continue
        r = o.result
        subject = r.subject or subject
        # running headers often print only the chapter number; keep the title we already know
        if r.chapter_number and r.chapter_number != ch_num:
            ch_num, ch_title = r.chapter_number, r.chapter_title
        elif r.chapter_title:
            ch_title = r.chapter_title
        chapter = " ".join(x for x in (ch_num, ch_title) if x) or None
        if o.review_reasons:
            review.append({"page": o.page.number, "reasons": o.review_reasons})
        for k in r.answer_key:
            key_entries.append((o.page.number, k.qid, k.answer))
        for it in r.items:
            fig_urls = [u for u in (figs.from_label(o, lab) for lab in it.figure_ids) if u]
            for mf in it.missing_figures:
                mcount[o.page.number] = mcount.get(o.page.number, 0) + 1
                u = figs.from_norm_box(o, mcount[o.page.number], mf)
                if u:
                    fig_urls.append(u)
            options = [{"label": op.label.strip().strip("()").lower(), "text": op.text,
                        "figures": [u for u in (figs.from_label(o, lab) for lab in op.figure_ids) if u]}
                       for op in it.options]
            if it.is_continuation:
                prev = next((p for p in reversed(recs) if p.kind == it.kind), recs[-1] if recs else None)
                if prev is not None:
                    prev.text = (prev.text + "\n" + it.text).strip()
                    prev.options.extend(options)
                    prev.figures.extend(fig_urls)
                    prev.pages.append(o.page.number)
                    if it.kind == "solution" and it.answer and not prev.item.answer:
                        prev.item.answer = it.answer
                    continue
            recs.append(Rec(it.kind, norm_qid(it.qid), o.page.number, subject, chapter, it,
                            it.text, options, fig_urls, [o.page.number]))

    # ---- build questions, then link solutions / answer keys to the nearest earlier question
    questions: list[dict] = []
    by_qid: dict[str, list[dict]] = {}
    orphans: list[dict] = []
    for rec in recs:
        if rec.kind == "question":
            q = {
                "uid": f"p{rec.page:04d}-{rec.qid or 'x'}-{len(questions) + 1}",
                "qid": rec.qid, "subject": rec.subject, "chapter": rec.chapter,
                "pages": rec.pages, "question_type": rec.item.question_type,
                "text": rec.text, "options": rec.options, "figures": rec.figures,
                "year": rec.item.year, "marks": rec.item.marks,
                "answer_key": None, "solution": None,
                "confidence": rec.item.confidence, "flags": [],
            }
            questions.append(q)
            if rec.qid:
                by_qid.setdefault(rec.qid, []).append(q)
        else:
            sol = {"qid": rec.qid, "answer": rec.item.answer, "text": rec.text,
                   "figures": rec.figures, "pages": rec.pages, "confidence": rec.item.confidence}
            cands = [q for q in by_qid.get(rec.qid or "", []) if q["solution"] is None
                     and q["pages"][0] <= rec.page]
            if cands:
                cands[-1]["solution"] = sol
            else:
                orphans.append(sol)
    for page, qid, ans in key_entries:
        cands = [q for q in by_qid.get(norm_qid(qid) or "", []) if q["answer_key"] is None
                 and q["pages"][0] <= page]
        if cands:
            cands[-1]["answer_key"] = ans
    # backfill subject within a chapter (the subject header may first appear a few pages in)
    by_ch: dict[str, str] = {}
    for q in questions:
        if q["chapter"] and q["subject"] and len(q["subject"]) > 3:  # skip branch codes like "CE"
            by_ch.setdefault(q["chapter"], q["subject"])
    for q in questions:
        if q["chapter"] in by_ch:  # one subject per chapter (alternate page headers can disagree)
            q["subject"] = by_ch[q["chapter"]]
    for q in questions:
        sol = q["solution"]
        if sol is not None and not sol.get("answer") and q["answer_key"]:
            sol["answer"] = q["answer_key"]
            sol["answer_source"] = "answer_key"
        q["flags"] = _flags_for(q)

    linked = sum(1 for q in questions if q["solution"])
    return {
        "questions": questions,
        "orphan_solutions": orphans,
        "pages_needing_review": review,
        "page_errors": page_errors,
        "stats": {
            "questions": len(questions),
            "with_solution": linked,
            "with_figures": sum(1 for q in questions if q["figures"] or any(o["figures"] for o in q["options"])),
            "flagged": sum(1 for q in questions if q["flags"]),
            "orphan_solutions": len(orphans),
            "figures_cropped": len(figs._done),
        },
    }
