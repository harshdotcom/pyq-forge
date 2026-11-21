---
name: pyq-page-transcribe
description: Manually transcribe a single question-bank page image into the pyqforge page JSON contract (questions, options, solutions, answer keys, figure labels, LaTeX). Use when an interactive agent (Claude Code or Codex) must extract or correct one page by hand, e.g. a page that failed in the pipeline, or to produce a gold-standard page for testing extraction accuracy.
---

# Transcribing a page by hand

The authoritative rules are the same ones the pipeline sends to the models:
- `pyqforge/prompts/page_extract.md` — field-by-field rules (read it first)
- `pyqforge/schema.py` — `PAGE_SCHEMA` (strict JSON Schema) and pydantic `PageResult`

## Steps
1. Make sure the page has been through detection, so F-labels exist:
   `.venv/bin/python -m pyqforge inspect BOOK.pdf --pages N` → view `out/<book>/_inspect/pNNNN.jpg`.
   (Labels are deterministic for a given render, so they match the pipeline's labels.)
2. Read the page image. Two-column pages: left column top→bottom, then right column.
3. Write the JSON object (`page_type`, `subject`, `chapter_number`, `chapter_title`, `items`,
   `answer_key`, `notes`). Key rules:
   - transcribe, never solve or correct the book
   - math as LaTeX (`$...$` inline, `$$...$$` display); options never inside `text`
   - option labels are bare letters `a`..`d`
   - year/marks from tags like `[2001 : 1 M]`
   - `is_continuation` only for unnumbered content at the top of a column continuing the
     previous page; `truncated` when the item runs off the bottom
   - attach figure labels to the item or to the specific option; real diagrams without a box go in
     `missing_figures` with a 0-1000 page-scale box
4. Validate before saving:
   ```bash
   .venv/bin/python -c "import json,sys;from pyqforge.schema import PageResult;PageResult.model_validate(json.load(open(sys.argv[1])));print('valid')" page.json
   ```
5. To feed it into a run, put it under `result` in `out/<book>/_work/pages/pNNNN.json`
   (copy the structure of a neighbouring cached page) and run `pyqforge assemble out/<book>`.
