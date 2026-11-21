"""Background extraction runs (each one a `python -m pyqforge extract` subprocess)."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .workspace import Workspace, WorkspaceError, now_iso

PROGRESS = re.compile(r"\[\s*(\d+)/(\d+)\]\s+page\s+(\d+):\s*(.*)")
ACTIVE = ("queued", "running")

# seconds per page with 1 worker, used for estimates until real runs exist
DEFAULT_SPP = {"claude": 12.0, "codex": 26.0, "auto": 18.0}


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # zombie check on Linux
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True


class JobManager:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._recover()

    # ------------------------------------------------------------ lifecycle
    def _recover(self) -> None:
        """Runs left 'running' by a previous server process: re-attach if alive, else mark."""
        for r in self.ws.list_runs():
            if r.get("status") in ACTIVE:
                if _alive(r.get("pid")):
                    threading.Thread(target=self._watch_detached, args=(r["id"],), daemon=True).start()
                else:
                    self._finish(r, None, interrupted=True)

    def start(self, cfg: dict) -> dict:
        slug = cfg["book"]
        meta = self.ws.book(slug)
        engine = cfg.get("engine", "claude")
        if engine not in ("claude", "codex", "auto"):
            raise WorkspaceError("engine must be claude, codex or auto")
        output = self.ws.output_name(slug, cfg.get("variant"))
        with self._lock:
            for r in self.ws.list_runs(book=slug):
                if r.get("status") in ACTIVE and r.get("output") == output:
                    raise WorkspaceError(f"a run is already writing to {output} ({r['id']})")
        pages_spec = (cfg.get("pages") or "").strip() or None
        n_pages = self.ws.page_count_for(slug, pages_spec)
        if n_pages == 0:
            raise WorkspaceError("page range selects no pages")

        cmd = [sys.executable, "-u", "-m", "pyqforge", "extract", str(self.ws.pdf_path(slug)),
               "--out", str(self.ws.out), "--book-name", output, "--engine", engine,
               "--workers", str(int(cfg.get("workers") or 3)),
               "--source-name", meta.get("original_name") or "source.pdf"]
        if pages_spec:
            cmd += ["--pages", pages_spec]
        if cfg.get("verify"):
            cmd += ["--verify", "--verify-engine", cfg.get("verify_engine") or "other"]
        for key, flag in (("claude_model", "--claude-model"), ("codex_model", "--codex-model"),
                          ("claude_effort", "--claude-effort"), ("codex_effort", "--codex-effort")):
            if cfg.get(key):
                cmd += [flag, str(cfg[key])]
        if cfg.get("force"):
            cmd.append("--force")

        run = {
            "id": self.ws.new_run_id(), "book": slug, "book_title": meta.get("title"), "output": output,
            "config": {k: cfg.get(k) for k in ("pages", "engine", "verify", "verify_engine", "claude_model",
                                               "codex_model", "claude_effort", "codex_effort", "workers",
                                               "force", "variant")},
            "pages_total": n_pages, "status": "queued", "created_at": now_iso(), "started_at": None,
            "ended_at": None, "pid": None, "returncode": None, "cmd": cmd,
            "progress": {"done": 0, "total": n_pages}, "summary": None,
        }
        log_path = self.ws.runs / f"{run['id']}.log"
        log = open(log_path, "w")
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(Path(__file__).parents[2]),
                                start_new_session=True)
        run.update(status="running", started_at=now_iso(), pid=proc.pid)
        self.ws.save_run(run)
        with self._lock:
            self._procs[run["id"]] = proc
        threading.Thread(target=self._watch, args=(run["id"], proc, log), daemon=True).start()
        return run

    def _watch(self, run_id: str, proc: subprocess.Popen, log) -> None:
        rc = proc.wait()
        log.close()
        with self._lock:
            self._procs.pop(run_id, None)
        self._finish(self.ws.get_run(run_id), rc)

    def _watch_detached(self, run_id: str) -> None:
        r = self.ws.get_run(run_id)
        while _alive(r.get("pid")):
            time.sleep(2)
        self._finish(self.ws.get_run(run_id), None)

    def _finish(self, run: dict, rc: int | None, interrupted: bool = False) -> None:
        if run.get("status") == "cancelled":
            run["ended_at"] = run.get("ended_at") or now_iso()
        elif interrupted:
            run["status"] = "interrupted"
        elif rc is None:
            run["status"] = "done" if self._log_has_done(run) else "interrupted"
        else:
            run["status"] = {0: "done", 3: "partial"}.get(rc, "failed")
        run["returncode"] = rc
        run["ended_at"] = run.get("ended_at") or now_iso()
        prog = self.progress(run)
        run["progress"] = {"done": prog["done"], "total": prog["total"]}
        run["summary"] = self._summary(run)
        self.ws.save_run(run)

    def cancel(self, run_id: str) -> dict:
        run = self.ws.get_run(run_id)
        if run.get("status") not in ACTIVE:
            return run
        pid = run.get("pid")
        if pid and _alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                pass
        run["status"] = "cancelled"
        run["ended_at"] = now_iso()
        self.ws.save_run(run)
        return run

    # ------------------------------------------------------------ reading
    def log_text(self, run_id: str, tail: int = 400) -> str:
        p = self.ws.runs / f"{run_id}.log"
        if not p.is_file():
            return ""
        lines = p.read_text(errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    def _log_has_done(self, run: dict) -> bool:
        return "Done in" in self.log_text(run["id"], tail=50)

    def progress(self, run: dict) -> dict:
        pages = []
        total = run.get("pages_total", 0)
        for line in self.log_text(run["id"], tail=100000).splitlines():
            m = PROGRESS.search(line)
            if m:
                total = int(m.group(2))
                status = m.group(4)
                state = ("error" if status.startswith("ERROR") else "cached" if status.startswith("cached")
                         else "review" if "review(" in status else "ok")
                eng = re.match(r"(claude|codex)(?:\+(claude|codex))?", status)
                secs = re.search(r"\s(\d+)s\s", status)
                pages.append({"page": int(m.group(3)), "state": state, "detail": status[:160],
                              "engine": eng.group(1) if eng else None,
                              "verified_by": eng.group(2) if eng and eng.group(2) else None,
                              "seconds": int(secs.group(1)) if secs else None})
        return {"done": len(pages), "total": total, "pages": pages}

    def _summary(self, run: dict) -> dict | None:
        rep = self.ws.out / run["output"] / "report.json"
        if not rep.is_file():
            return None
        try:
            r = json.loads(rep.read_text())
        except json.JSONDecodeError:
            return None
        return {"stats": r.get("stats"), "usage": r.get("usage_this_run"),
                "page_errors": len(r.get("page_errors", [])), "engines": r.get("engines_used")}

    def active(self) -> list[dict]:
        return [r for r in self.ws.list_runs() if r.get("status") in ACTIVE]

    def seconds_per_page(self, engine: str) -> float:
        """Measured average from finished runs (wall time x workers / processed pages)."""
        samples = []
        for r in self.ws.list_runs(limit=50):
            if r.get("status") not in ("done", "partial") or r["config"].get("engine") != engine:
                continue
            u = (r.get("summary") or {}).get("usage") or {}
            calls = u.get("calls") or 0
            if calls and u.get("seconds"):
                samples.append(u["seconds"] / max(1, r["progress"]["done"]))
        return round(sum(samples) / len(samples), 1) if samples else DEFAULT_SPP.get(engine, 18.0)

    def estimate(self, slug: str, pages: str | None, engine: str, workers: int, verify: bool,
                 variant: str | None = None, force: bool = False) -> dict:
        from ..ingest import parse_page_ranges
        selected = parse_page_ranges(pages or None, self.ws.book(slug)["pages"])
        done = set() if force else {p for p, st in self.ws.page_states(self.ws.output_name(slug, variant)).items()
                                     if st["state"] != "error"}
        todo = [p for p in selected if p not in done]
        n = len(todo)
        base = self.seconds_per_page(engine)
        measured = base != DEFAULT_SPP.get(engine, 18.0)
        spp = base * (1.25 if verify else 1.0)
        workers = max(1, int(workers or 1))
        calls = round(n * (1.25 if verify else 1.0))
        tokens_in = {"claude": 6000, "codex": 18000, "auto": 12000}.get(engine, 12000)
        return {"pages": n, "selected": len(selected), "reused": len(selected) - n,
                "seconds": round(n * spp / workers), "calls": calls,
                "input_tokens": calls * tokens_in, "output_tokens": calls * 2000,
                "seconds_per_page": round(spp, 1), "basis": "measured" if measured else "default"}


# ---------------------------------------------------------------- engine status (no token cost)
_ENGINE_CACHE: dict = {"t": 0.0, "data": None}


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def engine_status(jobs: JobManager | None = None, refresh: bool = False) -> dict:
    if not refresh and _ENGINE_CACHE["data"] and time.time() - _ENGINE_CACHE["t"] < 60:
        data = dict(_ENGINE_CACHE["data"])
    else:
        claude = {"name": "claude", "label": "Claude Code", "installed": bool(shutil.which("claude"))}
        if claude["installed"]:
            _, v = _run(["claude", "--version"])
            claude["version"] = v.splitlines()[0] if v else None
            rc, out = _run(["claude", "auth", "status", "--json"])
            try:
                st = json.loads(out[out.index("{"):])
            except (ValueError, json.JSONDecodeError):
                st = {}
            claude.update(
                logged_in=bool(st.get("loggedIn")),
                auth_method=st.get("authMethod"),
                subscription=st.get("authMethod") == "claude.ai",
                plan=st.get("subscriptionType") or st.get("planType"),
                account=st.get("email"), org=st.get("orgName"),
                expires_at=st.get("loginExpiresAt"),
                api_key_in_env=bool(os.environ.get("ANTHROPIC_API_KEY")),
            )
        codex = {"name": "codex", "label": "Codex", "installed": bool(shutil.which("codex"))}
        if codex["installed"]:
            _, v = _run(["codex", "--version"])
            codex["version"] = v.splitlines()[0] if v else None
            rc, out = _run(["codex", "login", "status"])
            last = out.splitlines()[-1] if out else ""
            codex.update(logged_in=rc == 0, auth_method=last,
                         subscription="chatgpt" in last.lower(),
                         api_key_in_env=bool(os.environ.get("OPENAI_API_KEY")))
            cfg = Path.home() / ".codex" / "config.toml"
            if cfg.is_file():
                txt = cfg.read_text(errors="replace")
                m = re.search(r'^model\s*=\s*"([^"]+)"', txt, re.M)
                e = re.search(r'^model_reasoning_effort\s*=\s*"([^"]+)"', txt, re.M)
                codex["default_model"] = m.group(1) if m else None
                codex["default_effort"] = e.group(1) if e else None
        data = {"claude": claude, "codex": codex, "checked_at": now_iso()}
        _ENGINE_CACHE.update(t=time.time(), data=data)
        data = dict(data)
    if jobs is not None:
        act = jobs.active()
        for name in ("claude", "codex"):
            mine = [r["id"] for r in act if r["config"].get("engine") in (name, "auto")
                    or (r["config"].get("verify") and r["config"].get("verify_engine") == "other")]
            data[name] = {**data[name], "active_runs": mine, "busy": bool(mine)}
    return data
