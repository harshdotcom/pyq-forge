---
name: pyq-review
description: Review and fix pyqforge output - flagged questions, missing solutions, wrong or missing figure crops, bad LaTeX - by comparing questions.json against the page images, editing the cached page JSON, and rebuilding. Use after a pyqforge extract run, or when the user reports a wrong question, wrong figure, or missing solution in questions.json.
---

# Reviewing pyqforge output

Files for a book live in `out/<book>/`:
- `report.json` → `flagged_questions`, `pages_needing_review`, `page_errors`
- `questions.json` → final output (never hand-edit; it is regenerated)
- `_work/pages/pNNNN.json` → the cached model result per page (**edit this**)
- `_work/llm_images/pNNNN.jpg` → the exact image the model saw, with F-labels
- `_work/renders/pNNNN.png` → 300-DPI page used for crops

## Workflow
0. Easiest visual check: `.venv/bin/python -m pyqforge ui` -> Review tab, filter "Flagged",
   and use the "Page N scan" button to compare against the original. (App outputs are in `workspace/out/`.)
1. Read `report.json`. Group problems by page.
2. For each page, view `_work/llm_images/pNNNN.jpg` and compare to the items in
   `_work/pages/pNNNN.json` → `result.items`.
3. Fix in the cache file, keeping the schema (see `pyqforge/schema.py`):
   - wrong text / LaTeX / options → edit `text` / `options`
   - figure attached to wrong item → move the label between `figure_ids` lists
   - figure missing or badly cropped → add to the item's `missing_figures` a box on the
     0-1000 page scale `{x0,y0,x1,y1,description}`; it is snapped to ink automatically
   - item split across pages → the second part must have `is_continuation: true`
   - wrong qid → fix `qid` exactly as printed
4. Rebuild without any LLM call:
   ```bash
   .venv/bin/python -m pyqforge assemble out/<book>
   ```
5. Alternatively re-run just the bad pages through the model (the other engine is often a
   good second opinion):
   delete `_work/pages/pNNNN.json`, then
   `.venv/bin/python -m pyqforge extract BOOK.pdf --out out --pages NNNN --engine codex`
   then re-run with the full original range (cached pages are free) so linking sees every page.

## Flag meanings
| flag | usual cause | fix |
|---|---|---|
| `no_solution` | solution page not in the run, or qid mismatch | include solution pages; fix qid |
| `figure_mentioned_but_missing` | detector missed the diagram and model did not report it | add `missing_figures` |
| `option_count_N` | options split across columns/pages or misread | fix options / continuation |
| `answer_not_in_options` | answer letter misread | check solution header e.g. "2.5 (b)" |
| `answer_key_mismatch` | answer table vs solution header disagree | check the page; books have errata |
| `low_confidence` | blurry scan or complex math | verify text against image |

Linking rule (for reasoning about `no_solution` / orphans): a solution attaches to the nearest
*earlier* question with the same normalised qid that has no solution yet.
