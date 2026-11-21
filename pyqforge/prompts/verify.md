Another model already extracted this page (PDF page {page_number}). Its JSON is below. Your job is to REVIEW it against the attached image and return a corrected JSON object in the same schema.

Check especially:
1. Every question/solution on the page is present, in reading order, with the correct printed `qid`.
2. Text, options and LaTeX match the page exactly (no hallucinated or missing terms, signs, units, exponents).
3. Figure labels (F1, F2, ...) are attached to the right item/option; real diagrams without a box are in `missing_figures`.
4. `answer`, `year`, `marks`, `is_continuation`, `truncated` are correct.
Keep everything that is already right. Set `confidence` honestly. Mention what you changed in `notes`.

Previous extraction:
```json
{previous_json}
```

---
The original extraction instructions follow, for reference.

{base_prompt}
