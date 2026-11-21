"""pyqforge internal web app (stdlib HTTP server, JSON API + single-page UI).

GET  /                                  the app
GET  /api/overview                      totals, engines, active + recent runs
GET  /api/engines[?refresh=1]           Claude / Codex login + activity (no token cost)
GET  /api/books                         library with per-book insights
POST /api/books/upload                  raw PDF body, header X-Filename
POST /api/books/import                  {"url": "..."} or {"path": "...", "copy": bool}
GET  /api/books/<slug>                  insights + outputs + runs
DELETE /api/books/<slug>
GET  /api/books/<slug>/pages[?output=]  per-page extraction state
GET  /api/books/<slug>/thumb/<n>        page thumbnail (jpg)
GET  /api/books/<slug>/pdf              the source PDF
POST /api/estimate                      {"book","pages","engine","workers","verify"}
GET  /api/runs[?book=]                  run history
POST /api/runs                          start a run
GET  /api/runs/<id>                     run + live progress + log tail
POST /api/runs/<id>/cancel
GET  /api/outputs/<output>/questions    questions.json of an output
GET  /static/<output>/<path>            figures and page images of an output
"""
from __future__ import annotations

import json
import mimetypes
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .jobs import JobManager, engine_status
from .workspace import Workspace, WorkspaceError

WEB = Path(__file__).parent / "web"


def make_handler(ws: Workspace, jobs: JobManager):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # -------------------------------------------------------- helpers
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _err(self, msg: str, code: int = 400) -> None:
            self._json({"error": msg}, code)

        def _file(self, path: Path, root: Path, cache: bool = False) -> None:
            try:
                rp = path.resolve()
                rp.relative_to(root.resolve())
            except (ValueError, OSError):
                return self._err("forbidden", 403)
            if not rp.is_file():
                return self._err("not found", 404)
            ctype = mimetypes.guess_type(rp.name)[0] or "application/octet-stream"
            data = rp.read_bytes()
            self._send(200, data, ctype, {"Cache-Control": "max-age=3600"} if cache else None)

        def _body_json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except json.JSONDecodeError as e:
                raise WorkspaceError("invalid JSON body") from e

        def _route(self) -> tuple[list[str], dict]:
            u = urlparse(self.path)
            return [unquote(p) for p in u.path.split("/") if p], {k: v[-1] for k, v in parse_qs(u.query).items()}

        def _safe(self, fn) -> None:
            try:
                fn()
            except WorkspaceError as e:
                self._err(str(e), 400)
            except Exception as e:  # keep the server alive
                traceback.print_exc()
                self._err(f"{type(e).__name__}: {e}", 500)

        # -------------------------------------------------------- GET
        def do_GET(self):  # noqa: N802
            self._safe(self._get)

        def do_HEAD(self):  # noqa: N802
            self._safe(self._get)

        def _get(self) -> None:
            p, q = self._route()
            if not p:
                return self._send(200, (WEB / "app.html").read_bytes(), "text/html; charset=utf-8")
            if p[0] == "static" and len(p) >= 3:
                return self._file(ws.out.joinpath(*p[1:]), ws.out, cache=True)
            if p[0] != "api":
                return self._err("not found", 404)
            p = p[1:]
            if p == ["overview"]:
                return self._json(self._overview())
            if p == ["engines"]:
                return self._json(engine_status(jobs, refresh=q.get("refresh") == "1"))
            if p == ["books"]:
                return self._json([ws.book_insights(b["slug"]) for b in ws.books()])
            if len(p) == 2 and p[0] == "books":
                ins = ws.book_insights(p[1])
                ins["run_list"] = ws.list_runs(book=p[1])
                return self._json(ins)
            if len(p) == 3 and p[0] == "books" and p[2] == "pages":
                outs = ws.outputs_for(p[1])
                out = q.get("output") or (outs[0] if outs else None)
                return self._json({"output": out, "pages": ws.book(p[1])["pages"],
                                   "states": ws.page_states(out) if out else {}})
            if len(p) == 4 and p[0] == "books" and p[2] == "thumb":
                w = min(1200, max(120, int(q.get("w", 360))))
                return self._file(ws.thumb(p[1], int(p[3]), w), ws.library, cache=True)
            if len(p) == 3 and p[0] == "books" and p[2] == "pdf":
                return self._file(ws.pdf_path(p[1]), Path("/"))
            if p == ["runs"]:
                return self._json(ws.list_runs(book=q.get("book"), limit=int(q.get("limit", 0)) or None))
            if len(p) == 2 and p[0] == "runs":
                run = ws.get_run(p[1])
                run["live"] = jobs.progress(run)
                run["log"] = jobs.log_text(p[1], tail=int(q.get("tail", 200)))
                return self._json(run)
            if len(p) == 3 and p[0] == "outputs" and p[2] == "questions":
                return self._file(ws.out / p[1] / "questions.json", ws.out)
            return self._err("not found", 404)

        def _overview(self) -> dict:
            books = [ws.book_insights(b["slug"]) for b in ws.books()]
            runs = ws.list_runs()
            tot = lambda k: sum((b["stats"] or {}).get(k, 0) for b in books)  # noqa: E731
            return {
                "totals": {
                    "books": len(books),
                    "pages_total": sum(b["book"]["pages"] for b in books),
                    "pages_extracted": sum(b["pages_extracted"] for b in books),
                    "questions": tot("questions"), "with_solution": tot("with_solution"),
                    "with_figures": tot("with_figures"), "flagged": tot("flagged"),
                    "figures": tot("figures_cropped"), "runs": len(runs),
                    "calls": sum(b["usage"]["calls"] for b in books),
                    "seconds": sum(b["usage"]["seconds"] for b in books),
                },
                "engines": engine_status(jobs),
                "active_runs": [dict(r, live=jobs.progress(r)) for r in jobs.active()],
                "recent_runs": runs[:8],
                "books": books[:6],
            }

        # -------------------------------------------------------- POST / DELETE
        def do_POST(self):  # noqa: N802
            self._safe(self._post)

        def _post(self) -> None:
            p, _ = self._route()
            if p == ["api", "books", "upload"]:
                n = int(self.headers.get("Content-Length") or 0)
                name = unquote(self.headers.get("X-Filename") or "upload.pdf")
                if n <= 0:
                    return self._err("empty upload")
                meta = ws.add_upload(Path(name).name, self.rfile, n)
                return self._json(meta, 201)
            if p == ["api", "books", "import"]:
                b = self._body_json()
                if b.get("url"):
                    return self._json(ws.add_from_url(b["url"].strip()), 201)
                if b.get("path"):
                    return self._json(ws.add_local_path(b["path"].strip(), bool(b.get("copy"))), 201)
                return self._err("give a url or a path")
            if p == ["api", "estimate"]:
                b = self._body_json()
                return self._json(jobs.estimate(b["book"], b.get("pages"), b.get("engine", "claude"),
                                                int(b.get("workers") or 3), bool(b.get("verify")),
                                                b.get("variant"), bool(b.get("force"))))
            if p == ["api", "runs"]:
                return self._json(jobs.start(self._body_json()), 201)
            if len(p) == 4 and p[:2] == ["api", "runs"] and p[3] == "cancel":
                return self._json(jobs.cancel(p[2]))
            return self._err("not found", 404)

        def do_DELETE(self):  # noqa: N802
            self._safe(self._delete)

        def _delete(self) -> None:
            p, _ = self._route()
            if len(p) == 3 and p[:2] == ["api", "books"]:
                if any(r["book"] == p[2] for r in jobs.active()):
                    return self._err("stop the running extraction first")
                ws.delete_book(p[2])
                return self._json({"deleted": p[2]})
            return self._err("not found", 404)

    return Handler


def serve_app(workspace: Path, host: str = "127.0.0.1", port: int = 8000) -> None:
    ws = Workspace(workspace)
    jobs = JobManager(ws)
    httpd = ThreadingHTTPServer((host, port), make_handler(ws, jobs))
    httpd.daemon_threads = True
    print(f"pyqforge app on http://{host}:{port}   workspace: {ws.root}   (Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
