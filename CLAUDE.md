# pyq-forge — agent notes (Codex / Claude Code)

Python 3.10+ CLI that extracts questions, figures and linked solutions from question-bank PDFs
using the `claude` and `codex` CLIs on subscription logins. See README.md.

- Run things with `.venv/bin/python -m pyqforge ...`; tests: `.venv/bin/python -m pytest -q`.
- Skills live in `skills/` (pyq-extract, pyq-review, pyq-page-transcribe), linked into
  `.claude/skills`, `.codex/skills`, `.agents/skills`. Use them.
- Engines: `--engine claude|codex|auto`; single-subscription runs use `--verify --verify-engine same`.
- Never add API keys or switch engines to API billing; engines deliberately strip
  ANTHROPIC_API_KEY / OPENAI_API_KEY from subprocess env.
- `pyqforge/schema.py` PAGE_SCHEMA must stay OpenAI-strict compatible (every property required,
  additionalProperties false, nullable via type arrays) — a test enforces it.
- Pixel coordinates come only from local code (`layout.py`); models pick F-labels or give rough
  0-1000 boxes that `refine_box` snaps to ink.
- `questions.json` is generated; fix data in `out/<book>/_work/pages/*.json` then `pyqforge assemble`.
- Web app: `pyqforge ui` (pyqforge/app/: workspace.py storage+insights, jobs.py runs+engine status,
  server.py JSON API, web/app.html single-page UI). Data in `workspace/`.
