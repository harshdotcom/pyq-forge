"""Engine registry + a pool that spreads work over both subscriptions and fails over."""
from __future__ import annotations

import itertools
import threading
from pathlib import Path

from .base import Engine, EngineError, EngineResult, NotLoggedIn, RateLimited, Usage
from .claude_cli import ClaudeCLIEngine
from .codex_cli import CodexCLIEngine

ENGINES: dict[str, type[Engine]] = {"claude": ClaudeCLIEngine, "codex": CodexCLIEngine}

__all__ = ["ENGINES", "Engine", "EngineError", "EngineResult", "EnginePool", "NotLoggedIn",
           "RateLimited", "Usage", "build_engine", "detect_available_engines"]


def detect_available_engines() -> list[str]:
    """Return names of engines whose CLIs are installed on PATH."""
    return [name for name, cls in ENGINES.items() if cls().available()]


def build_engine(name: str, model: str | None = None, effort: str | None = None,
                 timeout: int = 900) -> Engine:
    if name not in ENGINES:
        raise ValueError(f"unknown engine {name!r}; choose from {sorted(ENGINES)}")
    return ENGINES[name](model=model, effort=effort, timeout=timeout)


class AllEnginesExhausted(RuntimeError):
    pass


class EnginePool:
    """`--engine auto` round-robins pages across engines; when one hits its subscription
    limit (or is logged out) it is parked and the others carry on."""

    def __init__(self, engines: list[Engine]):
        if not engines:
            raise ValueError("no engines configured")
        self.engines = engines
        self._dead: dict[str, str] = {}
        self._rr = itertools.cycle(range(len(engines)))
        self._lock = threading.Lock()

    def alive(self) -> list[Engine]:
        return [e for e in self.engines if e.name not in self._dead]

    def other_than(self, name: str) -> Engine | None:
        for e in self.alive():
            if e.name != name:
                return e
        return None

    def run(self, system_prompt: str, prompt: str, image: Path, schema: dict,
            prefer: str | None = None, retries: int = 2) -> EngineResult:
        last: Exception | None = None
        tried: set[str] = set()
        while True:
            with self._lock:
                alive = [e for e in self.alive() if e.name not in tried]
                if not alive:
                    break
                if prefer and any(e.name == prefer for e in alive):
                    eng = next(e for e in alive if e.name == prefer)
                else:
                    eng = None
                    for _ in range(len(self.engines)):
                        cand = self.engines[next(self._rr)]
                        if cand in alive:
                            eng = cand
                            break
                    eng = eng or alive[0]
            for attempt in range(retries + 1):
                try:
                    return eng.run(system_prompt, prompt, image, schema)
                except (RateLimited, NotLoggedIn) as e:
                    with self._lock:
                        self._dead[eng.name] = f"{type(e).__name__}: {e}"
                    last = e
                    break
                except EngineError as e:
                    last = e
            tried.add(eng.name)
        raise AllEnginesExhausted(f"no engine could process {image.name}: {last}; parked={self._dead}")

    @property
    def parked(self) -> dict[str, str]:
        return dict(self._dead)
