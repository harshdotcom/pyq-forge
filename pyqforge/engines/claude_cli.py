"""Claude via the Claude Code CLI (`claude -p`) using the logged-in Pro/Max subscription.

The image is sent inline as a base64 content block through `--input-format stream-json`,
so no tool call is needed. Built-in tools, MCP servers, skills and session persistence are
all disabled to keep each call small (~1.5K tokens overhead) and side-effect free.
Do NOT set ANTHROPIC_API_KEY in the environment, or the CLI will bill the API instead.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path

from .base import Engine, EngineError, EngineResult, Usage


def _media_type(p: Path) -> str:
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp"}.get(p.suffix.lower(), "image/jpeg")


class ClaudeCLIEngine(Engine):
    name = "claude"
    binary = "claude"

    def _env(self) -> dict:
        env = dict(os.environ)
        # force subscription auth: an API key in the env would take precedence
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(k, None)
        return env

    def _cmd(self, system_prompt: str, schema: dict) -> list[str]:
        cmd = [self.resolved_binary, "-p",
               "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose",
               "--json-schema", json.dumps(schema),
               "--system-prompt", system_prompt,
               "--tools", "",
               "--no-session-persistence",
               "--strict-mcp-config",
               "--disable-slash-commands"]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        return cmd + self.extra_args

    def run(self, system_prompt: str, prompt: str, image: Path, schema: dict) -> EngineResult:
        b64 = base64.b64encode(image.read_bytes()).decode()
        msg = {"type": "user", "message": {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": _media_type(image), "data": b64}},
            {"type": "text", "text": prompt},
        ]}}
        t0 = time.time()
        try:
            proc = subprocess.run(self._cmd(system_prompt, schema), input=json.dumps(msg) + "\n",
                                  capture_output=True, text=True, timeout=self.timeout,
                                  env=self._env(), cwd=str(image.parent))
        except subprocess.TimeoutExpired as e:
            raise EngineError(f"claude timed out after {self.timeout}s") from e
        elapsed = time.time() - t0

        result = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                result = ev
        if result is None:
            raise self.classify_failure(proc.stderr + "\n" + proc.stdout[-2000:])
        if result.get("is_error") or result.get("subtype") != "success":
            raise self.classify_failure(json.dumps(result)[:3000] + "\n" + proc.stderr)

        data = result.get("structured_output")
        if data is None:
            try:
                data = json.loads(result.get("result") or "")
            except json.JSONDecodeError as e:
                raise EngineError("claude returned no structured output") from e

        u = result.get("usage") or {}
        usage = Usage(
            input_tokens=int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0)),
            cached_input_tokens=int(u.get("cache_read_input_tokens", 0)),
            output_tokens=int(u.get("output_tokens", 0)),
            api_equivalent_usd=float(result.get("total_cost_usd") or 0.0),
            seconds=elapsed, calls=1,
        )
        model = next(iter((result.get("modelUsage") or {}).keys()), self.model)
        return EngineResult(data=data, usage=usage, engine=self.name, model=model, raw=json.dumps(result))

    def doctor(self) -> tuple[bool, str]:
        if not self.available():
            return False, "`claude` not found on PATH (npm i -g @anthropic-ai/claude-code)"
        bin_path = self.resolved_binary
        # Fast auth check: claude auth status
        try:
            st_proc = subprocess.run([bin_path, "auth", "status", "--json"],
                                     capture_output=True, text=True, timeout=15, env=self._env())
            out = (st_proc.stdout + st_proc.stderr).strip()
            if "{" in out:
                st = json.loads(out[out.index("{"):])
                if not st.get("loggedIn"):
                    return False, "not logged in; run `claude` and /login"
                account = st.get("email")
                plan = st.get("subscriptionType") or st.get("planType") or "subscription"
                info = f"ok (logged in as {account}, {plan})" if account else f"ok (logged in, {plan})"
                return True, info
        except (subprocess.TimeoutExpired, OSError, ValueError, json.JSONDecodeError):
            pass

        try:
            proc = subprocess.run([bin_path, "-p", "Reply with the single word OK.",
                                   "--output-format", "json", "--tools", "",
                                   "--no-session-persistence", "--strict-mcp-config"]
                                  + (["--model", self.model] if self.model else []),
                                  capture_output=True, text=True, timeout=30, env=self._env())
        except subprocess.TimeoutExpired:
            return False, "claude did not answer within 30s"
        except OSError as e:
            return False, f"error running claude: {e}"
        try:
            res = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return False, (proc.stderr or proc.stdout)[-500:] or "no output; run `claude` and /login"
        if res.get("is_error"):
            return False, str(res.get("result"))[:500]
        model = next(iter((res.get("modelUsage") or {}).keys()), "?")
        return True, f"ok (model {model}, subscription login)"
