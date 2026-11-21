from pathlib import Path

import cv2
import numpy as np

from pyqforge.assemble import assemble, norm_answer, norm_qid
from pyqforge.engines import Usage
from pyqforge.extract import PageOutcome
from pyqforge.ingest import PageInfo, parse_page_ranges
from pyqforge.layout import Candidate, detect_figures
from pyqforge.schema import PAGE_SCHEMA, PageResult


def _walk(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_schema_is_openai_strict_compatible():
    for obj in _walk(PAGE_SCHEMA):
        assert obj.get("additionalProperties") is False
        assert set(obj["required"]) == set(obj["properties"])


def test_page_ranges():
    assert parse_page_ranges("3-5,9,1", 10) == [1, 3, 4, 5, 9]
    assert parse_page_ranges("8-20", 10) == [8, 9, 10]
    assert parse_page_ranges(None, 3) == [1, 2, 3]


def test_norm_qid():
    assert norm_qid("Q. 2.5") == "2.5"
    assert norm_qid(" 13.38. ") == "13.38"
    assert norm_qid(None) is None


def _blank_page(tmp: Path, n: int) -> PageInfo:
    img = np.full((3508, 2480, 3), 255, np.uint8)
    cv2.rectangle(img, (300, 300), (1100, 900), (0, 0, 0), 6)  # a "diagram"
    p = tmp / f"p{n:04d}.png"
    cv2.imwrite(str(p), img)
    return PageInfo(index=n - 1, number=n, is_digital=False, width_pt=595, height_pt=842, render_path=p)


def _item(kind, qid, **kw):
    base = dict(kind=kind, qid=qid, is_continuation=False, text="t", options=[], answer=None,
                year=None, marks=None, question_type="mcq", figure_ids=[], missing_figures=[],
                truncated=False, confidence=0.95)
    base.update(kw)
    return base


def _opts():
    return [{"label": x, "text": x.upper(), "figure_ids": []} for x in "abcd"]


def test_linking_continuation_and_figures(tmp_path):
    p1, p2, p3 = (_blank_page(tmp_path, n) for n in (1, 2, 3))
    r1 = PageResult.model_validate({
        "page_type": "questions", "subject": "SOM", "chapter_number": "2", "chapter_title": "SFD",
        "items": [_item("question", "2.1", options=_opts(), figure_ids=["F1"], text="As shown in figure"),
                  _item("question", "2.2", options=_opts()[:2], truncated=True)],
        "answer_key": [], "notes": ""})
    r2 = PageResult.model_validate({
        "page_type": "mixed", "subject": None, "chapter_number": None, "chapter_title": None,
        "items": [_item("question", None, is_continuation=True, options=_opts()[2:]),
                  _item("question", "2.3", question_type="nat")],
        "answer_key": [{"qid": "2.1", "answer": "b"}, {"qid": "2.2", "answer": "c"}], "notes": ""})
    r3 = PageResult.model_validate({
        "page_type": "solutions", "subject": None, "chapter_number": None, "chapter_title": None,
        "items": [_item("solution", "2.1", answer="b"), _item("solution", "2.2", answer="d"),
                  _item("solution", "9.9", answer="a")],
        "answer_key": [], "notes": ""})
    cands = [Candidate("F1", (300, 300, 1100, 900))]
    outs = [PageOutcome(p, cands, r, "claude", None, None, [], Usage()) for p, r in ((p1, r1), (p2, r2), (p3, r3))]
    data = assemble(outs, tmp_path / "out", "/static/book")
    qs = {q["qid"]: q for q in data["questions"]}
    assert set(qs) == {"2.1", "2.2", "2.3"}
    assert len(qs["2.2"]["options"]) == 4 and qs["2.2"]["pages"] == [1, 2]
    assert qs["2.1"]["figures"] == ["/static/book/figures/p0001_F1.png"]
    assert (tmp_path / "out" / "figures" / "p0001_F1.png").exists()
    assert qs["2.1"]["solution"]["answer"] == "b" and qs["2.1"]["answer_key"] == "b"
    assert "answer_key_mismatch" in qs["2.2"]["flags"]
    assert "no_solution" in qs["2.3"]["flags"]
    assert [o["qid"] for o in data["orphan_solutions"]] == ["9.9"]
    assert qs["2.1"]["chapter"] == "2 SFD" and qs["2.3"]["subject"] == "SOM"


def test_detector_finds_box(tmp_path):
    page = _blank_page(tmp_path, 7)
    cands = detect_figures(page.render_path)
    assert len(cands) == 1
    x0, y0, x1, y1 = cands[0].box
    assert x0 <= 300 and y0 <= 300 and x1 >= 1100 and y1 >= 900


def test_detector_keeps_dimension_labels(tmp_path):
    """Labels/dimension lines just outside the drawing (the '4 m' row) must be inside the crop."""
    img = np.full((3508, 2480, 3), 255, np.uint8)
    cv2.rectangle(img, (400, 400), (1400, 900), (0, 0, 0), 6)          # the drawing
    cv2.line(img, (400, 960), (1400, 960), (0, 0, 0), 3)               # dimension line below
    cv2.putText(img, "4 m", (850, 1010), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 3)
    cv2.putText(img, "A.", (320, 650), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 3)  # side label
    p = tmp_path / "dim.png"
    cv2.imwrite(str(p), img)
    (x0, y0, x1, y1), = [c.box for c in detect_figures(p)]
    assert y1 >= 1015, "dimension label below the drawing was cut off"
    assert x0 <= 320, "side label was cut off"


def test_norm_answer():
    assert norm_answer("18 N") == norm_answer("18") == "18"
    assert norm_answer("197.0") == "197"
    assert norm_answer("(b)") == "b"
    assert norm_answer("a, c") == norm_answer("(c) & (a)") == "a,c"
    assert norm_answer("b") != norm_answer("c")
