Extract every question and every solution on the attached page image (PDF page {page_number}).

## The image
- Coloured boxes labelled F1, F2, ... were drawn by a detector around regions that MAY be diagrams. Candidate labels on this page: {candidate_labels}.
- Some boxes are false positives (decorations, chapter numbers, logos, equations, header art). Ignore those.
- A box may contain a figure that belongs to an option, a question, or a solution: attach it to the right place.
- If a real diagram / graph / circuit / table-as-image has NO box (or its box cuts off a large part of it), add it to `missing_figures` of the item it belongs to, with an approximate box on a 0-1000 scale of the full page (x0,y0 top-left, x1,y1 bottom-right) and a few words of description.

## Reading order
Pages are often two-column. Read the left column top to bottom, then the right column top to bottom. Emit items in that order.

## Items
- A new item starts ONLY where a printed number badge/label appears (e.g. "2.17", "13.38 (b)"). Unnumbered lines such as "Hence correct option is (c)." belong to the item above them (or are a continuation from the previous page) — never invent a qid from position or sequence.
- "Common Data", "Linked Answer" or "Statement for Questions X and Y" blocks: put the shared statement at the start of EACH listed question's `text` (prefixed "Common data: ") and put the shared figure labels in EACH of those questions' `figure_ids`. Do not attach them to the question printed before the block.
- `kind`: "question" for a problem statement, "solution" for an answer/explanation block (e.g. "13.38 (b)" followed by working).
- `qid`: the printed number exactly, e.g. "2.5", "13.38", "Q.12". Null only for a continuation whose number is not on this page.
- `is_continuation`: true ONLY for content at the very top of a column that continues an item started on the previous page (no number printed before it). Put that content in `text` (and `options` if any). Do not mark it if the item number is printed.
- `truncated`: true if the item visibly continues beyond the bottom of this page.
- `text`: full statement/explanation in Markdown. Math in LaTeX: inline `$...$`, display `$$...$$`. Keep line breaks of derivations as separate lines. Do NOT include the options inside `text` for questions. Do not include the year/marks tag.
- `options`: for MCQ/MSQ, each option with `label` as the bare letter ("a","b","c","d") and its text; empty list when there are no options. An option that is a picture gets its F-label in that option's `figure_ids` (text may be "").
- `answer`: for solutions, the option letter(s) or numeric answer printed next to the qid, e.g. "b", "a, b", "4.1", "*" (bonus/no option). Null for questions.
- `year` / `marks`: from tags like "[2001 : 1 M]" -> 2001 and 1. Null if absent.
- `question_type`: mcq (single correct), msq (multiple correct), nat (numerical answer, no options), descriptive, unknown.
- `figure_ids`: F-labels of figures that belong to the statement/explanation (not to individual options).
- `confidence`: 0-1, your confidence that this item was transcribed and attributed correctly.

## Page-level
- `page_type`: questions | solutions | answer_key | mixed | other (cover, index, blank, ads).
- `subject`: the academic subject from the running header (e.g. "Solid Mechanics", "Fluid Mechanics & Fluid Machines"), else null. Never the book title, publisher, exam name or branch code (e.g. not "GATE", "CE", "ME").
- `chapter_number` / `chapter_title`: if a chapter heading or running header shows them, else null.
- `answer_key`: if the page has an answer table ("2.1 (b) 2.2 (c) ..."), list every pair here (do not also create items for them).
- `notes`: anything unusual (illegible areas, cut-off text), else "".

{text_hint_block}Return only the JSON object.
