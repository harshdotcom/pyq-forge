# pyq-forge

Turn a question-bank / previous-year-papers PDF — **born-digital or scanned** — into a JSON file of
questions with their **diagrams cropped from the page** and **linked solutions**, using your
**Claude (Pro/Max) and/or ChatGPT (Plus/Pro) subscriptions** through the `claude` and `codex` CLIs.
No API keys.

```
PDF ─► ingest ─► detect figures (OpenCV, local) ─► label F1..Fn on page ─► claude -p / codex exec
    ─► validate (pydantic) ─► optional cross-check by the other engine ─► link Q↔solution ─► JSON + PNGs
```

## Setup
```bash
cd pyq-forge
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"   # or: python -m venv .venv && .venv/bin/pip install -e ".[dev]"
claude            # then /login with your Claude Pro/Max account (once)
codex login       # sign in with ChatGPT (once)
.venv/bin/python -m pyqforge doctor
```
Keep `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` unset; pyqforge also removes them from the CLI
subprocess environment so calls always go through the subscription login.

## Usage
```bash
# free preview of figure detection
.venv/bin/python -m pyqforge inspect book.pdf --pages 20-28

# extract (one chapter: question pages + its solution pages)
.venv/bin/python -m pyqforge extract book.pdf --out out --pages 20-28 --engine auto --verify

# whole book; re-run the same command to resume after a usage limit
.venv/bin/python -m pyqforge extract book.pdf --out out --engine auto --verify

# rebuild JSON from cached page results after manual fixes (no LLM)
.venv/bin/python -m pyqforge assemble out/<book>

# web app: upload, run, monitor, review — http://127.0.0.1:8000
.venv/bin/python -m pyqforge ui
```

### Web app (recommended)
```bash
.venv/bin/python -m pyqforge ui          # http://127.0.0.1:8000  (workspace/ folder next to it)
```
A clean internal UI over the whole pipeline — no extra dependencies (Python stdlib server):

| Screen | What you can do |
|---|---|
| **Overview** | library totals (pages extracted / total, questions, figures, flagged), which agent is signed in and working right now, live runs, recent runs |
| **Library** | add PDFs by drag-and-drop or file picker (any size, with upload progress), by pasting a **https link** (downloaded to local storage), a **local file path** (linked or copied), or **Ctrl+V** with a copied PDF |
| **Book** | pages extracted vs total, a **page map** of every page (extracted / needs review / failed / not extracted; hover for details, click to preview), start an extraction (page range shortcuts: all, not yet extracted, failed, needs review, pilot; agent Claude / Codex / both; cross-check by the other or same agent; model, effort, parallelism, output name), live estimate (time, calls, tokens; already-done pages reused), outputs, run history |
| **Run** | live progress bar, per-page result and agent, time left, log, stop button (finished pages are kept; re-run continues) |
| **Review** | browse questions per output with LaTeX, figures, options, answer, solution, and the original page scan |
| **Agents** | Claude / Codex sign-in method, plan, account, default model, current activity (read from `claude auth status` / `codex login status` — no usage spent) |

Everything lives in `workspace/`: `library/<book>/source.pdf`, `out/<book>[--variant]/` (same format as the CLI),
`runs/<id>.json|.log`. The server binds to 127.0.0.1 only. Runs started from the UI keep going if the
browser is closed; if the server restarts, running jobs are re-attached (or marked interrupted).

### Running with Codex only (ChatGPT subscription)
```bash
codex login                                   # once, choose "Sign in with ChatGPT"
.venv/bin/python -m pyqforge doctor           # codex line must say "Logged in using ChatGPT"
.venv/bin/python -m pyqforge extract book.pdf --out out --pages 20-28 \
    --engine codex --verify --verify-engine same            # no Claude involved at all
# optional: --codex-model <model>  --codex-effort low|medium|high
```
`--verify-engine same` makes Codex re-check its own doubtful pages; without it, `--verify` asks
the other subscription (Claude). Claude-only is the mirror image: `--engine claude --verify --verify-engine same`.
To compare engines on the same pages, give each run its own `--book-name`
(e.g. `--book-name mybook-codex`) and switch between them in the viewer's book dropdown.

You can also drive everything from inside Codex or Claude Code: both agents load the
project skills (`.codex/skills`, `.agents/skills`, `.claude/skills` all point at `skills/`), so
asking either one to "extract pages 20-28 of book.pdf with codex" follows the `pyq-extract` skill.

| option | default | notes |
|---|---|---|
| `--engine claude\|codex\|auto` | `claude` | `auto` spreads pages over both subscriptions and fails over when one is rate-limited/logged out |
| `--verify` | off | pages with doubtful items are re-checked and corrected |
| `--verify-engine other\|same` | `other` | re-check with the other subscription, or with the same engine (single-subscription runs) |
| `--claude-effort` / `--codex-effort` | `--effort` | per-engine reasoning effort |
| `--book-name` | PDF name | output folder name, e.g. to keep a Claude run and a Codex run side by side |
| `--claude-model` | `sonnet` | any `claude --model` alias/id (`opus` for max accuracy) |
| `--codex-model` | codex config | any `codex -m` model |
| `--effort` | CLI default | `low` / `medium` / `high` for both |
| `--workers` | 3 | parallel pages |
| `--static-prefix` | `/static/<book>` | URL prefix written into figure paths |
| `--pages` | all | `20-40,55` |
| `--force` | off | ignore cache |

## Output
`out/<book>/questions.json`
```jsonc
{
  "schema_version": 1, "source_pdf": "book.pdf", "static_prefix": "/static/book",
  "stats": {"questions": 52, "with_solution": 51, "with_figures": 18, "flagged": 4, ...},
  "questions": [{
    "uid": "p0020-2.5-5", "qid": "2.5",
    "subject": "Solid Mechanics", "chapter": "2 Shear Force and Bending Moment",
    "pages": [20], "question_type": "mcq",
    "text": "The bending moment (in kNm units) at the mid span location X ...",
    "options": [{"label": "a", "text": "0", "figures": []}, ...],
    "figures": ["/static/book/figures/p0020_F5.png"],
    "year": 2001, "marks": 1,
    "answer_key": "b",
    "solution": {"qid": "2.5", "answer": "b", "text": "$$R_A + R_B = 30$$ ...",
                 "figures": [], "pages": [25], "confidence": 0.9},
    "confidence": 0.95, "flags": []
  }],
  "orphan_solutions": []
}
```
- `out/<book>/figures/*.png` — crops from a 300-DPI render. `pNNNN_Fk.png` = detector box,
  `pNNNN_Mk.png` = figure the model located itself (snapped to ink).
- `out/<book>/report.json` — stats, tokens/time used this run, flagged questions, pages needing review, errors.
- `out/<book>/_work/` — renders, the exact images sent to the models, and the per-page cache.

Flags: `no_solution`, `figure_mentioned_but_missing`, `option_count_N`, `answer_not_in_options`,
`answer_key_mismatch`, `low_confidence`.

## How it works (and why)
1. **Ingest** (`ingest.py`): PyMuPDF renders each page at 300 DPI. Pages with a real text layer
   are "digital": their text is passed to the model as a spelling hint, and embedded images +
   vector-drawing clusters become figure candidates. Scans (or OCR'd scans) use the image only.
2. **Figure candidates** (`layout.py`): OpenCV finds diagram-like ink (large 2-D components,
   long horizontal + vertical strokes), merges nearby parts, splits on whitespace, absorbs labels.
   Over-detection is fine; equations/logos are ignored by the model.
3. **Set-of-marks prompt** (`extract.py`, `prompts/`): the page (1600 px long edge) with labelled
   boxes goes to the model; it returns strict JSON (`schema.py`) saying which label belongs to which
   question/option/solution, and gives approximate boxes for any unboxed diagram. LLMs choose
   labels reliably but are poor at pixel coordinates — so pixels always come from local code.
4. **Engines** (`engines/`): `claude -p` with inline base64 image via stream-json, `--json-schema`,
   no tools/MCP/session; `codex exec -i img --output-schema` in an empty temp dir, read-only sandbox.
   `EnginePool` handles round-robin, retry, and parking an engine on rate limit / logout.
5. **Assemble** (`assemble.py`): merges cross-page continuations, crops figures, links each solution
   / answer-key entry to the nearest *earlier* question with the same number (works for books that
   restart numbering per chapter or subject), and flags anomalies.

## Cost / usage (measured on the pilot — see report.json for yours)
Pilot: *Made Easy GATE PYQ 2024*, pages 20-28 (chapter 2, scanned), `--engine auto --verify`, 3 workers:
24/24 questions found, 23 linked to solutions (the 24th has no solution printed in the book),
18 with figures, 1 flag, **88 s total**, 11 CLI calls.

| per page call | input tokens | output tokens | time |
|---|---|---|---|
| Claude (`sonnet`) | ~6K | 1-2K | ~10 s |
| Codex (default model) | ~18K (≈15K is Codex's fixed agent prompt) | ~1.2K | ~25 s |
| page + `--verify` cross-check | ~25K | ~3.4K | ~40 s |

Projection for 100 pages: ~110-125 calls, ~15-20 min with 3 workers. Claude-only ≈ 0.6M in /
0.2M out tokens ≈ **$3.6 API-equivalent** (Claude reports this per call; pyqforge sums it).
On subscriptions the cash cost per book is **$0** beyond the plan; the constraint is the plan's
5-hour / weekly usage windows — `--engine auto` spreads the load over both subscriptions.

## Terms of use
Subscription logins are intended for the subscriber's own use. Running this for your own/internal
digitisation is the intended pattern; serving it as a product to other users needs API keys
(add an engine in `engines/` — the `Engine` interface is 2 methods).

## Agent skills
`skills/` holds three skills, linked for both agents (`.claude/skills` for Claude Code,
`.codex/skills` and `.agents/skills` for Codex — verified that Codex lists all three):
- **pyq-extract** — run/pilot/resume the pipeline and report results
- **pyq-review** — fix flagged items via the page cache and rebuild
- **pyq-page-transcribe** — hand-transcribe one page into the same JSON contract

## Tests
```bash
.venv/bin/python -m pytest -q
```
