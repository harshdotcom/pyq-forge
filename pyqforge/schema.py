"""The page-level contract between the pipeline and any LLM engine.

The JSON Schema below is written to satisfy BOTH:
  * Claude Code `--json-schema`
  * Codex `--output-schema` (OpenAI strict structured outputs: every property
    required, additionalProperties false, nullability via type arrays).
Pydantic models mirror it for validation after the model responds.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


def _nullable(t: str) -> dict:
    return {"type": [t, "null"]}


_STR_LIST = {"type": "array", "items": {"type": "string"}}

_BOX = {
    "type": "object",
    "additionalProperties": False,
    "required": ["x0", "y0", "x1", "y1", "description"],
    "properties": {
        "x0": {"type": "number"}, "y0": {"type": "number"},
        "x1": {"type": "number"}, "y1": {"type": "number"},
        "description": {"type": "string"},
    },
}

_OPTION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["label", "text", "figure_ids"],
    "properties": {
        "label": {"type": "string"},
        "text": {"type": "string"},
        "figure_ids": _STR_LIST,
    },
}

_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "qid", "is_continuation", "text", "options", "answer", "year",
                 "marks", "question_type", "figure_ids", "missing_figures", "truncated",
                 "confidence"],
    "properties": {
        "kind": {"type": "string", "enum": ["question", "solution"]},
        "qid": _nullable("string"),
        "is_continuation": {"type": "boolean"},
        "text": {"type": "string"},
        "options": {"type": "array", "items": _OPTION},
        "answer": _nullable("string"),
        "year": _nullable("integer"),
        "marks": _nullable("number"),
        "question_type": {"type": "string", "enum": ["mcq", "msq", "nat", "descriptive", "unknown"]},
        "figure_ids": _STR_LIST,
        "missing_figures": {"type": "array", "items": _BOX},
        "truncated": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
}

PAGE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page_type", "subject", "chapter_number", "chapter_title", "items",
                 "answer_key", "notes"],
    "properties": {
        "page_type": {"type": "string",
                      "enum": ["questions", "solutions", "answer_key", "mixed", "other"]},
        "subject": _nullable("string"),
        "chapter_number": _nullable("string"),
        "chapter_title": _nullable("string"),
        "items": {"type": "array", "items": _ITEM},
        "answer_key": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["qid", "answer"],
                "properties": {"qid": {"type": "string"}, "answer": {"type": "string"}},
            },
        },
        "notes": {"type": "string"},
    },
}


# ------------------------------------------------------------- pydantic mirror
class MissingFigure(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float
    description: str = ""


class Option(BaseModel):
    label: str
    text: str
    figure_ids: list[str] = Field(default_factory=list)


class Item(BaseModel):
    kind: Literal["question", "solution"]
    qid: str | None = None
    is_continuation: bool = False
    text: str = ""
    options: list[Option] = Field(default_factory=list)
    answer: str | None = None
    year: int | None = None
    marks: float | None = None
    question_type: Literal["mcq", "msq", "nat", "descriptive", "unknown"] = "unknown"
    figure_ids: list[str] = Field(default_factory=list)
    missing_figures: list[MissingFigure] = Field(default_factory=list)
    truncated: bool = False
    confidence: float = 0.0


class KeyEntry(BaseModel):
    qid: str
    answer: str


class PageResult(BaseModel):
    page_type: Literal["questions", "solutions", "answer_key", "mixed", "other"]
    subject: str | None = None
    chapter_number: str | None = None
    chapter_title: str | None = None
    items: list[Item] = Field(default_factory=list)
    answer_key: list[KeyEntry] = Field(default_factory=list)
    notes: str = ""
