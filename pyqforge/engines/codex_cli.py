"""OpenAI models via the Codex CLI (`codex exec`) using the logged-in ChatGPT subscription.

Runs in an empty temp dir with a read-only sandbox and an ephemeral session so the agent
cannot touch the project and no AGENTS.md is pulled into context.
Do NOT set OPENAI_API_KEY / CODEX_API_KEY, or Codex may bill the API instead.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .base import Engine, EngineError, EngineResult, Usage


class CodexCLIEngine(Engine):
    name = "codex"
    binary = "codex"

    def _env(self) -> dict:
        env = dict(os.environ)
        for k in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            env.pop(k, None)
        return env

    def run(self, system_prompt: str, prompt: str, image: Path, schema: dict) -> EngineResult:
        with tempfile.TemporaryDirectory(prefix="pyqforge-codex-") as td:
            tdp = Path(td)
            img = tdp / image.name
            shutil.copy(image, img)
            schema_path = tdp / "schema.json"
            schema_path.write_text(json.dumps(schema))
            out_path = tdp / "out.json"
            cmd = [self.binary, "exec",
                   "-i", str(img),
                   "--output-schema", str(schema_path),
                   "-o", str(out_path),
                   "--skip-git-repo-check",
                   "--sandbox", "read-only",
                   "--ephemeral",
                   "--json",
                   "-C", str(tdp)]
            if self.model:
                cmd += ["-m", self.model]
            if self.effort:
                cmd += ["-c", f"model_reasoning_effort={self.effort}"]
            cmd += self.extra_args + ["-"]
            full_prompt = f"{system_prompt}\n\n{prompt}\n\nThe page image is attached. Do not run any shell commands."
            t0 = time.time()
            try:
                proc = subprocess.run(cmd, input=full_prompt, capture_output=True, text=True,
                                      timeout=self.timeout, env=self._env(), cwd=td)
            except subprocess.TimeoutExpired as e:
                raise EngineError(f"codex timed out after {self.timeout}s") from e
            elapsed = time.time() - t0

            usage = Usage(seconds=elapsed, calls=1)
            errors: list[str] = []
            for line in proc.stdout.splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type", "")
                if t == "turn.completed" and isinstance(ev.get("usage"), dict):
                    u = ev["usage"]
                    usage.input_tokens += int(u.get("input_tokens", 0)) - int(u.get("cached_input_tokens", 0))
                    usage.cached_input_tokens += int(u.get("cached_input_tokens", 0))
                    usage.output_tokens += int(u.get("output_tokens", 0))
                elif t in ("error", "turn.failed"):
                    errors.append(json.dumps(ev))

            text = out_path.read_text() if out_path.exists() else ""
            if not text.strip():
                raise self.classify_failure("\n".join(errors) + "\n" + proc.stderr[-2000:])
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                raise EngineError(f"codex returned non-JSON: {text[:300]}") from e
            return EngineResult(data=data, usage=usage, engine=self.name, model=self.model, raw=text)

    def doctor(self) -> tuple[bool, str]:
        if not self.available():
            return False, "`codex` not found on PATH (npm i -g @openai/codex)"
        st = subprocess.run([self.binary, "login", "status"], capture_output=True, text=True,
                            timeout=60, env=self._env())
        msg = (st.stdout + st.stderr).strip()
        if st.returncode != 0:
            return False, msg[-500:] or "not logged in; run `codex login`"
        if "api key" in msg.lower():
            return True, f"WARNING: logged in with an API key, not ChatGPT: {msg}"
        return True, msg.splitlines()[-1] if msg else "ok"
