"""Local workspace: the PDF library, extraction outputs, run history and insights.

workspace/
  library/<slug>/source.pdf      uploaded / imported PDF (or a symlink for local imports)
  library/<slug>/meta.json       original name, pages, size, source, digital/scanned sample
  library/<slug>/thumbs/         page thumbnails (generated on demand)
  out/<output>/                  pyqforge extract output (questions.json, figures/, _work/)
                                 <output> is <slug> or <slug>--<variant> (e.g. --codex)
  runs/<run_id>.json / .log      one record + log per extraction run
"""
from __future__ import annotations

import json
import re
import shutil
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

from ..ingest import parse_page_ranges

MAX_DOWNLOAD = 2 * 1024 ** 3  # 2 GB


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "book"


class WorkspaceError(ValueError):
    pass


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.library = self.root / "library"
        self.out = self.root / "out"
        self.runs = self.root / "runs"
        for d in (self.library, self.out, self.runs):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ library
    def _new_slug(self, name: str) -> str:
        base = slugify(Path(name).stem)
        slug, i = base, 2
        while (self.library / slug).exists():
            slug, i = f"{base}-{i}", i + 1
        return slug

    def _finalize(self, slug: str, original_name: str, source: str, extra: dict | None = None) -> dict:
        pdf = self.library / slug / "source.pdf"
        try:
            doc = pymupdf.open(pdf)
        except Exception as e:
            shutil.rmtree(self.library / slug, ignore_errors=True)
            raise WorkspaceError(f"not a readable PDF: {e}") from e
        sample = list(range(min(doc.page_count, 12)))
        digital = sum(1 for i in sample if len(doc[i].get_text("text").strip()) > 40
                      and not any((b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1])
                                  > 0.8 * doc[i].rect.width * doc[i].rect.height
                                  for b in doc[i].get_image_info()))
        md = doc.metadata or {}
        meta = {
            "slug": slug,
            "original_name": original_name,
            "title": (md.get("title") or "").strip() or Path(original_name).stem,
            "pages": doc.page_count,
            "size_bytes": pdf.stat().st_size,
            "kind": "digital" if digital > len(sample) / 2 else ("mixed" if digital else "scanned"),
            "producer": md.get("producer") or "",
            "source": source,
            "added_at": now_iso(),
            **(extra or {}),
        }
        doc.close()
        (self.library / slug / "meta.json").write_text(json.dumps(meta, indent=2))
        return meta

    def add_upload(self, filename: str, stream, length: int) -> dict:
        if not filename.lower().endswith(".pdf"):
            raise WorkspaceError("only .pdf files are accepted")
        slug = self._new_slug(filename)
        d = self.library / slug
        d.mkdir(parents=True)
        dst = d / "source.pdf"
        remaining = length
        with open(dst, "wb") as f:
            first = True
            while remaining > 0:
                chunk = stream.read(min(1 << 20, remaining))
                if not chunk:
                    break
                if first and not chunk.startswith(b"%PDF"):
                    f.close()
                    shutil.rmtree(d, ignore_errors=True)
                    raise WorkspaceError("file does not look like a PDF")
                first = False
                f.write(chunk)
                remaining -= len(chunk)
        if remaining > 0:
            shutil.rmtree(d, ignore_errors=True)
            raise WorkspaceError("upload was interrupted")
        return self._finalize(slug, filename, "upload")

    def add_from_url(self, url: str) -> dict:
        if not re.match(r"^https?://", url, re.I):
            raise WorkspaceError("only http(s) links are supported")
        req = urllib.request.Request(url, headers={"User-Agent": "pyqforge/0.1"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            name = Path(urllib.request.url2pathname(resp.url.split("?")[0])).name or "download.pdf"
            cd = resp.headers.get("Content-Disposition", "")
            m = re.search(r'filename="?([^";]+)"?', cd)
            if m:
                name = m.group(1)
            if not name.lower().endswith(".pdf"):
                name += ".pdf"
            slug = self._new_slug(name)
            d = self.library / slug
            d.mkdir(parents=True)
            total = 0
            with open(d / "source.pdf", "wb") as f:
                first = True
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    if first and not chunk.startswith(b"%PDF"):
                        f.close()
                        shutil.rmtree(d, ignore_errors=True)
                        raise WorkspaceError("the link did not return a PDF (it may need a login "
                                             "or be an HTML viewer page)")
                    first = False
                    total += len(chunk)
                    if total > MAX_DOWNLOAD:
                        f.close()
                        shutil.rmtree(d, ignore_errors=True)
                        raise WorkspaceError("file larger than 2 GB")
                    f.write(chunk)
        return self._finalize(slug, name, "url", {"url": url})

    def add_local_path(self, path: str, copy: bool = False) -> dict:
        p = Path(path).expanduser()
        if not p.is_file() or p.suffix.lower() != ".pdf":
            raise WorkspaceError(f"no PDF at {p}")
        slug = self._new_slug(p.name)
        d = self.library / slug
        d.mkdir(parents=True)
        if copy:
            shutil.copy2(p, d / "source.pdf")
        else:
            (d / "source.pdf").symlink_to(p.resolve())
        return self._finalize(slug, p.name, "local-copy" if copy else "local-link", {"path": str(p.resolve())})

    def books(self) -> list[dict]:
        out = []
        for d in sorted(self.library.iterdir()):
            mf = d / "meta.json"
            if mf.is_file():
                out.append(json.loads(mf.read_text()))
        out.sort(key=lambda m: m.get("added_at", ""), reverse=True)
        return out

    @staticmethod
    def check_slug(slug: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", slug or ""):
            raise WorkspaceError("bad book id")
        return slug

    def book(self, slug: str) -> dict:
        mf = self.library / self.check_slug(slug) / "meta.json"
        if not mf.is_file():
            raise WorkspaceError(f"unknown book {slug}")
        return json.loads(mf.read_text())

    def pdf_path(self, slug: str) -> Path:
        return self.library / self.check_slug(slug) / "source.pdf"

    def delete_book(self, slug: str, with_outputs: bool = True) -> None:
        self.book(slug)
        shutil.rmtree(self.library / slug)
        if with_outputs:
            for o in self.outputs_for(slug):
                shutil.rmtree(self.out / o, ignore_errors=True)

    def thumb(self, slug: str, page: int, width: int = 360) -> Path:
        d = self.library / self.check_slug(slug) / "thumbs"
        d.mkdir(exist_ok=True)
        p = d / f"p{page:04d}_{width}.jpg"
        if not p.exists():
            with pymupdf.open(self.pdf_path(slug)) as doc:
                if not 1 <= page <= doc.page_count:
                    raise WorkspaceError("page out of range")
                pg = doc[page - 1]
                pix = pg.get_pixmap(matrix=pymupdf.Matrix(width / pg.rect.width, width / pg.rect.width))
                pix.save(p, jpg_quality=80)
        return p

    # ------------------------------------------------------------ outputs + insights
    def outputs_for(self, slug: str) -> list[str]:
        return sorted(p.name for p in self.out.iterdir()
                      if p.is_dir() and (p.name == slug or p.name.startswith(slug + "--")))

    def output_name(self, slug: str, variant: str | None) -> str:
        v = slugify(variant) if variant else ""
        return f"{slug}--{v}" if v else slug

    def page_states(self, output: str) -> dict[int, dict]:
        pages = {}
        for f in (self.out / output / "_work" / "pages").glob("p*.json"):
            try:
                d = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            r = d.get("result")
            pages[d["page"]] = {
                "state": "error" if not r else ("review" if d.get("review_reasons") else "ok"),
                "type": r.get("page_type") if r else None,
                "items": len(r.get("items", [])) if r else 0,
                "engine": d.get("engine"), "verified_by": d.get("verified_by"),
                "error": (d.get("error") or "")[:200] if not r else None,
            }
        return pages

    def output_insights(self, output: str) -> dict:
        od = self.out / output
        q = od / "questions.json"
        rep = od / "report.json"
        stats = {}
        if q.is_file():
            try:
                stats = json.loads(q.read_text()).get("stats", {})
            except json.JSONDecodeError:
                pass
        states = self.page_states(output)
        ok = sum(1 for s in states.values() if s["state"] != "error")
        info = {
            "output": output,
            "has_questions": q.is_file(),
            "pages_extracted": ok,
            "pages_failed": sum(1 for s in states.values() if s["state"] == "error"),
            "pages_review": sum(1 for s in states.values() if s["state"] == "review"),
            "engines": sorted({s["engine"] for s in states.values() if s["engine"]}),
            "stats": stats,
            "updated_at": datetime.fromtimestamp(q.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            if q.is_file() else None,
        }
        if rep.is_file():
            try:
                info["flagged_questions"] = len(json.loads(rep.read_text()).get("flagged_questions", []))
            except json.JSONDecodeError:
                pass
        return info

    def book_insights(self, slug: str) -> dict:
        meta = self.book(slug)
        outs = [self.output_insights(o) for o in self.outputs_for(slug)]
        best = max(outs, key=lambda o: (o["pages_extracted"], o["stats"].get("questions", 0)), default=None)
        runs = self.list_runs(book=slug)
        usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0, "api_equivalent_usd": 0.0}
        for r in runs:
            for k, v in ((r.get("summary") or {}).get("usage") or {}).items():
                if k in usage:
                    usage[k] += v
        return {
            "book": meta,
            "outputs": outs,
            "primary_output": best["output"] if best else None,
            "pages_extracted": best["pages_extracted"] if best else 0,
            "coverage": round((best["pages_extracted"] / meta["pages"]) if best and meta["pages"] else 0, 4),
            "stats": best["stats"] if best else {},
            "usage": usage,
            "runs": len(runs),
            "last_run": runs[0] if runs else None,
        }

    # ------------------------------------------------------------ runs
    def run_path(self, run_id: str) -> Path:
        if not re.fullmatch(r"r[0-9a-z-]+", run_id):
            raise WorkspaceError("bad run id")
        return self.runs / f"{run_id}.json"

    def save_run(self, run: dict) -> None:
        tmp = self.run_path(run["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(run, indent=2))
        tmp.replace(self.run_path(run["id"]))

    def get_run(self, run_id: str) -> dict:
        p = self.run_path(run_id)
        if not p.is_file():
            raise WorkspaceError("unknown run")
        return json.loads(p.read_text())

    def list_runs(self, book: str | None = None, limit: int | None = None) -> list[dict]:
        runs = []
        for f in self.runs.glob("r*.json"):
            try:
                r = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            if book and r.get("book") != book:
                continue
            runs.append(r)
        runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return runs[:limit] if limit else runs

    def new_run_id(self) -> str:
        return "r" + time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"

    def page_count_for(self, slug: str, spec: str | None) -> int:
        return len(parse_page_ranges(spec, self.book(slug)["pages"]))
