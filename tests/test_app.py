import io

import pymupdf
import pytest

from pyqforge.app.jobs import JobManager
from pyqforge.app.workspace import Workspace, WorkspaceError


def _pdf_bytes(pages=3) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"Question {i + 1}. What is 2 + 2? (a) 3 (b) 4 (c) 5 (d) 6 " * 3)
    return doc.tobytes()


def test_upload_and_insights(tmp_path):
    ws = Workspace(tmp_path / "ws")
    data = _pdf_bytes(3)
    meta = ws.add_upload("My Book 2024.pdf", io.BytesIO(data), len(data))
    assert meta["slug"] == "my-book-2024" and meta["pages"] == 3 and meta["kind"] == "digital"
    again = ws.add_upload("My Book 2024.pdf", io.BytesIO(data), len(data))
    assert again["slug"] == "my-book-2024-2"
    ins = ws.book_insights("my-book-2024")
    assert ins["pages_extracted"] == 0 and ins["coverage"] == 0 and ins["outputs"] == []
    assert ws.thumb("my-book-2024", 1).exists()
    ws.delete_book("my-book-2024-2")
    assert [b["slug"] for b in ws.books()] == ["my-book-2024"]


def test_rejects_bad_input(tmp_path):
    ws = Workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        ws.add_upload("notes.txt", io.BytesIO(b"x"), 1)
    with pytest.raises(WorkspaceError):
        ws.add_upload("fake.pdf", io.BytesIO(b"hello world"), 11)
    for bad in ("../etc", "a/b", "", "UPPER"):
        with pytest.raises(WorkspaceError):
            ws.book(bad)
    with pytest.raises(WorkspaceError):
        ws.add_from_url("file:///etc/passwd")


def test_estimate_and_runs(tmp_path):
    ws = Workspace(tmp_path / "ws")
    data = _pdf_bytes(10)
    ws.add_upload("b.pdf", io.BytesIO(data), len(data))
    jobs = JobManager(ws)
    e = jobs.estimate("b", "2-5", "claude", 2, False)
    assert e["pages"] == 4 and e["reused"] == 0 and e["basis"] == "default" and e["seconds"] > 0
    with pytest.raises(WorkspaceError):
        jobs.start({"book": "b", "pages": "50-60", "engine": "claude"})
    run = {"id": ws.new_run_id(), "book": "b", "output": "b", "status": "running", "pid": 999999,
           "config": {"engine": "claude"}, "created_at": "2026-01-01T00:00:00+00:00", "pages_total": 4,
           "progress": {"done": 0, "total": 4}}
    ws.save_run(run)
    JobManager(ws)  # recovery: dead pid -> interrupted
    assert ws.get_run(run["id"])["status"] == "interrupted"
