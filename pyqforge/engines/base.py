from __future__ import annotations

import abc
import shutil
from dataclasses import dataclass, field
from pathlib import Path


class EngineError(RuntimeError):
    """The engine ran but did not return usable output (retryable)."""


class RateLimited(EngineError):
    """Subscription usage window exhausted - stop using this engine for now."""


class NotLoggedIn(EngineError):
    """CLI is not authenticated with a subscription account."""


RATE_LIMIT_MARKERS = ("usage limit", "rate limit", "rate_limit", "429", "limit reached",
                      "quota", "too many requests", "try again at", "resets at")
AUTH_MARKERS = ("not logged in", "please log in", "login required", "unauthorized",
                "invalid api key", "authentication", "/login", "codex login")


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    api_equivalent_usd: float = 0.0   # what the same call would cost on the API (Claude reports it)
    seconds: float = 0.0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.api_equivalent_usd += other.api_equivalent_usd
        self.seconds += other.seconds
        self.calls += other.calls

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class EngineResult:
    data: dict
    usage: Usage
    engine: str
    model: str | None
    raw: str = field(default="", repr=False)


class Engine(abc.ABC):
    name: str = "base"
    binary: str = ""

    def __init__(self, model: str | None = None, effort: str | None = None,
                 timeout: int = 900, extra_args: list[str] | None = None):
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.extra_args = extra_args or []

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    @abc.abstractmethod
    def run(self, system_prompt: str, prompt: str, image: Path, schema: dict) -> EngineResult:
        ...

    @abc.abstractmethod
    def doctor(self) -> tuple[bool, str]:
        """Cheap live check: returns (ok, message)."""

    @staticmethod
    def classify_failure(text: str) -> EngineError:
        low = text.lower()
        if any(m in low for m in AUTH_MARKERS):
            return NotLoggedIn(text[-800:])
        if any(m in low for m in RATE_LIMIT_MARKERS):
            return RateLimited(text[-800:])
        return EngineError(text[-800:])
