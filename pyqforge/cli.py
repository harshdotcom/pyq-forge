"""pyqforge command line.

  pyqforge doctor
  pyqforge inspect BOOK.pdf --pages 20-25            # free: detection preview only
  pyqforge extract BOOK.pdf --out out/ --pages 20-40 --engine auto --verify
  pyqforge extract BOOK.pdf --pages 20-40 --engine codex --verify --verify-engine same   # Codex only
  pyqforge assemble out/<book>                       # rebuild JSON from cache (no LLM)
  pyqforge ui                                        # web app on http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .assemble import assemble
from .engines import ENGINES, EnginePool, Usage, build_engine
from .extract import PageExtractor, PageOutcome
from .ingest import load_pages, page_count, parse_page_ranges
from .layout import annotate_for_llm, detect_figures

log = logging.getLogger("pyqforge")


def slugify(name: str) -> str:
    if re.fullmatch(r"[a-z0-9]+(?:-{1,2}[a-z0-9]+)*", name):
        return name  # already a safe folder name (keeps output variants like book--codex)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "book"


def _engines_from_args(a) -> list:
    names = list(ENGINES) if a.engine == "auto" else [a.engine]
    models = {"claude": a.claude_model, "codex": a.codex_model}
    efforts = {"claude": a.claude_effort or a.effort, "codex": a.codex_effort or a.effort}
    mk = lambda n: build_engine(n, model=models[n], effort=efforts[n], timeout=a.timeout)  # noqa: E731
    engines = [mk(n) for n in names]
    if a.verify and a.engine != "auto" and a.verify_engine == "other":
        # a cross-check by the other subscription (use --verify-engine same to stay single-engine)
        engines.append(mk("codex" if a.engine == "claude" else "claude"))
    return [e for e in engines if e.available()] or engines


# ---------------------------------------------------------------- commands
def cmd_doctor(a) -> int:
    ok_all = True
    for name in ENGINES:
        eng = build_engine(name)
        ok, msg = eng.doctor()
        ok_all &= ok
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {msg}")
    import os
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY"):
        if os.environ.get(k):
            print(f"[note] {k} is set in your shell; pyqforge strips it from the CLI subprocess "
                  f"so the subscription login is used.")
    return 0 if ok_all else 1


def cmd_inspect(a) -> int:
    pdf = Path(a.pdf)
    out = Path(a.out) / slugify(pdf.stem) / "_inspect"
    nums = parse_page_ranges(a.pages, page_count(pdf))
    pages = load_pages(pdf, out.parent / "_work", nums)
    for p in pages:
        c = detect_figures(p.render_path, p.digital_boxes)
        dst = annotate_for_llm(p.render_path, c, out / f"p{p.number:04d}.jpg", 1600)
        print(f"page {p.number:>4}  {'digital' if p.is_digital else 'scanned':8} {len(c):>2} candidates -> {dst}")
    return 0


def _write_outputs(book_dir: Path, pdf: Path | None, outcomes: list[PageOutcome], static_prefix: str,
                   meta: dict) -> dict:
    data = assemble(outcomes, book_dir, static_prefix)
    usage = Usage()
    for o in outcomes:
        usage.add(o.usage)
    doc = {
        "schema_version": 1,
        "generator": f"pyqforge {__version__}",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_pdf": meta.get("source_pdf") or (pdf.name if pdf else None),
        "pages_processed": sorted(o.page.number for o in outcomes),
        "static_prefix": static_prefix,
        **{k: v for k, v in meta.items() if k != "source_pdf"},
        "stats": data["stats"],
        "questions": data["questions"],
        "orphan_solutions": data["orphan_solutions"],
    }
    (book_dir / "questions.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    report = {
        "stats": data["stats"],
        "usage_this_run": usage.as_dict(),
        "pages_needing_review": data["pages_needing_review"],
        "page_errors": data["page_errors"],
        "flagged_questions": [{"uid": q["uid"], "qid": q["qid"], "pages": q["pages"], "flags": q["flags"]}
                              for q in data["questions"] if q["flags"]],
        "engines_used": sorted({o.engine for o in outcomes if o.engine}),
    }
    (book_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def cmd_extract(a) -> int:
    pdf = Path(a.pdf)
    if not pdf.exists():
        print(f"no such file: {pdf}", file=sys.stderr)
        return 2
    slug = slugify(a.book_name or pdf.stem)
    book_dir = Path(a.out) / slug
    work = book_dir / "_work"
    nums = parse_page_ranges(a.pages, page_count(pdf))
    print(f"pyqforge: {pdf.name} -> {book_dir}  pages={len(nums)}  engine={a.engine}  "
          f"verify={a.verify}{'(' + a.verify_engine + ')' if a.verify else ''}  workers={a.workers}")
    t0 = time.time()
    pages = load_pages(pdf, work, nums)
    print(f"  rendered {len(pages)} pages in {time.time() - t0:.1f}s "
          f"({sum(p.is_digital for p in pages)} digital, {sum(not p.is_digital for p in pages)} scanned)")

    engines = _engines_from_args(a)
    pool = EnginePool(engines)
    prefer = None if a.engine == "auto" else a.engine
    ex = PageExtractor(pool, work, verify=a.verify, min_conf=a.min_confidence, prefer=prefer, force=a.force,
                       verify_same=(a.verify_engine == "same"))

    def progress(o: PageOutcome, done: int, total: int) -> None:
        if o.error:
            status = f"ERROR {o.error[:120]}"
        elif o.usage.calls == 0:
            status = "cached"
        else:
            n = len(o.result.items) if o.result else 0
            status = (f"{o.engine}{'+' + o.verified_by if o.verified_by else ''} {n} items "
                      f"{o.usage.seconds:.0f}s in={o.usage.input_tokens + o.usage.cached_input_tokens} "
                      f"out={o.usage.output_tokens}")
            if o.review_reasons:
                status += f" review({len(o.review_reasons)})"
        print(f"  [{done:>4}/{total}] page {o.page.number:>4}: {status}", flush=True)

    outcomes = ex.process_all(pages, workers=a.workers, on_done=progress)
    static_prefix = a.static_prefix or f"/static/{slug}"
    # the output covers every page extracted so far, not only this run's pages
    earlier = load_cached_outcomes(work, skip={o.page.number for o in outcomes})
    if earlier:
        print(f"  merging {len(earlier)} page(s) extracted in earlier runs")
    report = _write_outputs(book_dir, pdf, outcomes + earlier, static_prefix, {
        "source_pdf": a.source_name, "engine_mode": a.engine, "verify": a.verify, "verify_engine": a.verify_engine if a.verify else None,
    })
    u = report["usage_this_run"]
    print(f"\nDone in {time.time() - t0:.0f}s -> {book_dir / 'questions.json'}")
    print(f"  stats: {json.dumps(report['stats'])}")
    print(f"  usage this run: calls={u['calls']} input={u['input_tokens']} cached={u['cached_input_tokens']} "
          f"output={u['output_tokens']}  (Claude API-equivalent ${u['api_equivalent_usd']:.2f}; "
          f"subscription cost $0)")
    if pool.parked:
        print(f"  parked engines (limit/login): {pool.parked}")
    errors = report["page_errors"]
    if errors:
        print(f"  {len(errors)} page(s) failed; re-run the same command to retry only those.")
    return 0 if not errors else 3


def load_cached_outcomes(work: Path, skip: set[int] | None = None) -> list[PageOutcome]:
    """Every page result cached for an output (optionally skipping some page numbers)."""
    from .ingest import PageInfo
    from .layout import Candidate
    from .schema import PageResult
    outcomes = []
    for f in sorted((work / "pages").glob("p*.json")):
        d = json.loads(f.read_text())
        n = d["page"]
        if skip and n in skip:
            continue
        page = PageInfo(index=n - 1, number=n, is_digital=d.get("is_digital", False), width_pt=0,
                        height_pt=0, render_path=work / "renders" / f"p{n:04d}.png")
        outcomes.append(PageOutcome(
            page=page, candidates=[Candidate(c["label"], tuple(c["box"])) for c in d["candidates"]],
            result=PageResult.model_validate(d["result"]) if d.get("result") else None,
            engine=d.get("engine"), model=d.get("model"), verified_by=d.get("verified_by"),
            review_reasons=d.get("review_reasons", []), usage=Usage(), error=d.get("error")))
    return outcomes


def cmd_assemble(a) -> int:
    book_dir = Path(a.book_dir)
    work = book_dir / "_work"
    outcomes = load_cached_outcomes(work)
    if not outcomes:
        print(f"no cached pages under {work}", file=sys.stderr)
        return 2
    report = _write_outputs(book_dir, None, outcomes, a.static_prefix or f"/static/{book_dir.name}",
                            {"source_pdf": None})
    print(f"rebuilt {book_dir / 'questions.json'}: {json.dumps(report['stats'])}")
    return 0


def cmd_ui(a) -> int:
    from .app.server import serve_app
    serve_app(Path(a.workspace), a.host, a.port)
    return 0


# ---------------------------------------------------------------- parser
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pyqforge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check claude/codex CLIs are installed and subscription-logged-in")

    sp = sub.add_parser("inspect", help="preview figure detection (no LLM, free)")
    sp.add_argument("pdf")
    sp.add_argument("--pages")
    sp.add_argument("--out", default="out")

    sp = sub.add_parser("extract", help="extract questions + solutions + figures to JSON")
    sp.add_argument("pdf")
    sp.add_argument("--out", default="out", help="output root (default: out/)")
    sp.add_argument("--pages", help="e.g. 20-40,55 (default: all)")
    sp.add_argument("--engine", choices=["claude", "codex", "auto"], default="claude",
                    help="auto = spread pages over both subscriptions with failover")
    sp.add_argument("--verify", action="store_true",
                    help="re-check pages that look doubtful with the OTHER engine")
    sp.add_argument("--claude-model", default="sonnet", help="claude alias/id (sonnet, opus, ...)")
    sp.add_argument("--codex-model", default=None, help="codex model (default: your codex config)")
    sp.add_argument("--verify-engine", choices=["other", "same"], default="other",
                    help="who re-checks doubtful pages: the other subscription (default) or the same engine")
    sp.add_argument("--effort", default=None, help="reasoning effort passed to both CLIs (low/medium/high)")
    sp.add_argument("--claude-effort", default=None, help="override --effort for claude")
    sp.add_argument("--codex-effort", default=None,
                    help="override --effort for codex (minimal/low/medium/high/xhigh, per your codex version)")
    sp.add_argument("--source-name", default=None, help="original file name to record in questions.json")
    sp.add_argument("--book-name", default=None,
                    help="output folder name under --out (default: from the PDF name), e.g. to compare engines")
    sp.add_argument("--workers", type=int, default=3, help="parallel pages (default 3)")
    sp.add_argument("--timeout", type=int, default=900, help="seconds per LLM call")
    sp.add_argument("--min-confidence", type=float, default=0.75)
    sp.add_argument("--static-prefix", default=None, help="URL prefix for figures (default /static/<book>)")
    sp.add_argument("--force", action="store_true", help="ignore cached page results")

    for name in ("ui", "serve"):
        sp = sub.add_parser(name, help="web app: upload books, run extractions, review results"
                            + (" (alias of ui)" if name == "serve" else ""))
        sp.add_argument("--workspace", default="workspace", help="folder for books, outputs and run history")
        sp.add_argument("--host", default="127.0.0.1", help="keep 127.0.0.1 for local-only access")
        sp.add_argument("--port", type=int, default=8000)

    sp = sub.add_parser("assemble", help="rebuild questions.json from cached page results (no LLM)")
    sp.add_argument("book_dir")
    sp.add_argument("--static-prefix", default=None)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    return {"doctor": cmd_doctor, "inspect": cmd_inspect, "extract": cmd_extract,
            "assemble": cmd_assemble, "ui": cmd_ui, "serve": cmd_ui}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
