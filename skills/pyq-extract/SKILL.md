---
name: pyq-extract
description: Run the pyqforge pipeline to turn a question-bank PDF (digital or scanned) into questions.json with cropped figures and linked solutions, using Claude Code and/or Codex subscription logins. Use when asked to extract, digitise, convert or process a PYQ / question-bank / solved-papers PDF, run a pilot, resume a run, or estimate how long or how much usage a book will take.
---

# Running pyqforge

All commands run from the repository root with the project venv:
`.venv/bin/python -m pyqforge <command>` (or `pyqforge <command>` if installed).

## 1. Preflight (always)
```bash
.venv/bin/python -m pyqforge doctor
```
Both lines should be `[OK]`. If `claude` fails: the user runs `claude` then `/login` with their Pro/Max account.
If `codex` fails: the user runs `codex login` (ChatGPT sign-in). Never ask for or set API keys —
the engines strip `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` on purpose so subscriptions are used.

## 2. Free preview of figure detection
```bash
.venv/bin/python -m pyqforge inspect BOOK.pdf --pages 20-25
```
Open `out/<book>/_inspect/pNNNN.jpg`. Boxes F1..Fn are figure candidates. False positives are fine
(the model ignores them); missing diagrams are recovered by the model's `missing_figures`.

## 3. Pilot before a full book
Pick one complete chapter: its question pages AND its answer/solution pages (solutions only link
if both are in the run). Use a thumbnail look at the PDF to find the range.
```bash
.venv/bin/python -m pyqforge extract BOOK.pdf --out out --pages 20-28 --engine auto --verify
```
Read the final summary line: per-page tokens and seconds tell you the cost of the full book
(multiply by page count). Claude prints an API-equivalent $ figure; subscription cost is $0 but
counts against the plan's 5-hour/weekly limits.

## 4. Engine choices
| flag | meaning |
|---|---|
| `--engine claude` | all pages through `claude -p` (default model `--claude-model sonnet`) |
| `--engine codex` | all pages through `codex exec` (model from codex config or `--codex-model`) |
| `--engine auto` | round-robin pages over both subscriptions; if one hits its limit it is parked and the other continues |
| `--verify` | pages with doubtful items are re-checked and corrected |
| `--verify-engine same` | the re-check uses the same engine (Codex-only or Claude-only runs); default `other` uses the other subscription |
| `--codex-effort` / `--claude-effort` | per-engine reasoning effort (overrides `--effort`) |
| `--book-name NAME` | output folder name, e.g. keep `book` (Claude) and `book-codex` (Codex) side by side |
| `--effort low/medium/high` | reasoning effort for both CLIs |
| `--workers N` | parallel pages (3 is safe; more burns limits faster) |

### Codex-only recipe
```bash
.venv/bin/python -m pyqforge extract BOOK.pdf --out out --pages 20-28 --engine codex --verify --verify-engine same
```
Requires `codex login` with ChatGPT (doctor shows "Logged in using ChatGPT"). Expect ~18K input
tokens per call (Codex's own agent prompt is ~15K of that) and ~25 s per page.

## 5. Full book / resume
Same command without `--pages` (or a big range). Every page result is cached in
`out/<book>/_work/pages/`. If a limit is hit, the run ends with page errors; re-run the identical
command later and only failed/missing pages are processed. `--force` re-does everything.

## 6. Outputs
- `out/<book>/questions.json` — the deliverable (schema in README.md).
- `out/<book>/figures/*.png` — static crops, referenced as `<static-prefix>/figures/...`.
- `out/<book>/report.json` — stats, usage, flagged questions, pages needing review, page errors.
- Web app: `.venv/bin/python -m pyqforge ui` -> http://127.0.0.1:8000 — upload/paste PDFs, start and
  monitor runs, page map, agents status, review. Its data lives in `workspace/` (`library/`, `out/`, `runs/`);
  for CLI runs that should appear in the app use `--out workspace/out --book-name <slug>`.
After a run, summarise for the user: questions found, % with solution, % with figures, flagged
count, errors, tokens/time per page. Then offer the `pyq-review` skill for flagged items.
