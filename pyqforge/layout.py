"""Local (free) figure detection, page annotation and cropping.

The LLM never produces pixel crops directly. Instead:
  1. OpenCV proposes candidate figure regions (over-generating is fine).
  2. Candidates are drawn on the page as labelled boxes F1, F2, ...
  3. The LLM says which labels belong to which question/option/solution.
  4. Crops are cut from the clean 300-DPI render.
If the LLM sees a figure with no box, it returns an approximate box (0-1000 scale),
which `refine_box` snaps to the actual ink on the page.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 in render pixels


@dataclass
class Candidate:
    label: str
    box: Box


# ---------------------------------------------------------------- helpers
def _ink_mask(gray: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    mask = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, 41, 18)
    return mask


def _union(a: Box, b: Box) -> Box:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _overlap(a: Box, b: Box, pad: int = 0) -> bool:
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1])


def _area(b: Box) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _merge_boxes(boxes: list[Box], pad: int) -> list[Box]:
    boxes = list(boxes)
    changed = True
    while changed:
        changed = False
        out: list[Box] = []
        while boxes:
            cur = boxes.pop()
            i = 0
            while i < len(boxes):
                if _overlap(cur, boxes[i], pad):
                    cur = _union(cur, boxes.pop(i))
                    changed = True
                else:
                    i += 1
            out.append(cur)
        boxes = out
    return boxes


def _split_on_gaps(mask: np.ndarray, r: Box, gap: int) -> list[Box]:
    """Split a region where a horizontal band of >= gap empty rows separates ink."""
    x0, y0, x1, y1 = r
    sub = mask[y0:y1, x0:x1]
    rows = (sub > 0).sum(axis=1) <= 1
    parts, start, run = [], None, 0
    for i, empty in enumerate(rows):
        if not empty:
            if start is None:
                start = i
            run = 0
        else:
            run += 1
            if start is not None and run >= gap:
                parts.append((start, i - run + 1))
                start = None
    if start is not None:
        parts.append((start, len(rows)))
    if len(parts) <= 1:
        return [r]
    out = []
    for a, b in parts:
        cols = np.where((sub[a:b] > 0).any(axis=0))[0]
        if len(cols):
            out.append((x0 + int(cols[0]), y0 + a, x0 + int(cols[-1]) + 1, y0 + b))
    return out


def _is_text_band(ink: np.ndarray, ya: int, yb: int, x0: int, x1: int, margin: int) -> bool:
    """Rows [ya, yb) look like body text: their ink continues well past the box sides."""
    if yb <= ya:
        return False
    H, W = ink.shape
    rows = ink[ya:yb]
    outside = int(rows[:, :max(0, x0 - margin)].any(axis=0).sum() + rows[:, min(W, x1 + margin):].any(axis=0).sum())
    return outside > 0.25 * max(1, x1 - x0)


def _is_glyph_row(ink: np.ndarray, ya: int, yb: int, x0: int, x1: int, th: float) -> bool:
    """Rows [ya, yb) inside the box are a line of running text: many letter-sized pieces spread
    across the width and no long strokes (labels are sparse; dimension rows contain lines)."""
    if yb - ya < 2:
        return False
    band = (ink[ya:yb, x0:x1] * 255).astype(np.uint8)
    n, _, st, _ = cv2.connectedComponentsWithStats(band, connectivity=8)
    glyphs = [r for r in st[1:] if r[4] >= 4]
    if len(glyphs) < 10:
        return False
    if any(w > 3.5 * th or h > 1.8 * th for _, _, w, h, _ in glyphs):
        return False  # contains a stroke / drawing part
    xs = sorted(x for x, *_ in glyphs)
    return (xs[-1] - xs[0]) > 0.55 * (x1 - x0)


def _trim_text_edges(mask: np.ndarray, b: Box, th: float) -> Box:
    """Cut a partial body-text line off the top/bottom edge of a figure box."""
    ink = mask > 0
    x0, y0, x1, y1 = b
    margin, gap, depth = int(1.5 * th), max(2, int(0.3 * th)), int(2.5 * th)

    def first_gap(rng):
        run = 0
        for i, y in enumerate(rng):
            if int(ink[y, x0:x1].sum()) <= 1:
                run += 1
                if run >= gap:
                    return i - run + 1
            else:
                run = 0
        return None

    for _ in range(2):  # a box edge can cut through up to two text lines
        d = first_gap(range(y0, min(y1, y0 + depth)))
        if d and (_is_text_band(ink, y0, y0 + d, x0, x1, margin) or _is_glyph_row(ink, y0, y0 + d, x0, x1, th)):
            y0 = y0 + d
            while y0 < y1 and int(ink[y0, x0:x1].sum()) <= 1:
                y0 += 1
        d = first_gap(range(y1 - 1, max(y0, y1 - depth), -1))
        if d and (_is_text_band(ink, y1 - d, y1, x0, x1, margin) or _is_glyph_row(ink, y1 - d, y1, x0, x1, th)):
            y1 = y1 - d
            while y1 > y0 and int(ink[y1 - 1, x0:x1].sum()) <= 1:
                y1 -= 1
    return (x0, y0, x1, y1)


def _grow_to_whitespace(mask: np.ndarray, b: Box, th: float, gap: float = 0.6,
                        cap: float = 3.5, gap_x: float = 1.5) -> Box:
    """Push each side outward while ink crosses it (dimension lines, labels, arrows),
    stopping at the first clean whitespace band `gap*th` thick. A side that would grow
    more than `cap*th` is left where it was (it is running into body text)."""
    H, W = mask.shape
    ink = mask > 0
    band = max(2, int(gap * th))
    band_x = max(2, int(gap_x * th))
    limit = max(4, int(cap * th))
    x0, y0, x1, y1 = b

    def busy(region: np.ndarray) -> bool:
        return int(region.sum()) > 2

    def text_line(ya: int, yb: int) -> bool:
        """Rows [ya,yb) added above/below: a body-text line continues well past the figure's
        sides, while labels and dimension lines stay within them."""
        if yb <= ya:
            return False
        rows = ink[ya:yb]
        span = max(1, x1 - x0)
        outside = int(rows[:, :max(0, x0 - band_x)].any(axis=0).sum() + rows[:, min(W, x1 + band_x):].any(axis=0).sum())
        return outside > 0.25 * span

    def text_col(xa: int, xb: int) -> bool:
        """Columns [xa,xb) added sideways belong to a paragraph that runs above/below the box."""
        if xb <= xa:
            return False
        cols = ink[:, xa:xb]
        outside = int(cols[:max(0, y0 - band), :].any(axis=1)[-int(3 * th):].sum()
                      + cols[min(H, y1 + band):, :].any(axis=1)[:int(3 * th)].sum())
        return outside > 1.5 * th

    for _ in range(4):
        before = (x0, y0, x1, y1)
        # bottom
        g, ny = 0, y1
        while ny < H and g <= limit and busy(ink[ny:min(H, ny + band), x0:x1]):
            ny += 1; g += 1
        if g <= limit and not text_line(y1, ny):
            y1 = ny
        # top
        g, ny = 0, y0
        while ny > 0 and g <= limit and busy(ink[max(0, ny - band):ny, x0:x1]):
            ny -= 1; g += 1
        if g <= limit and not text_line(ny, y0):
            y0 = ny
        # right
        g, nx = 0, x1
        while nx < W and g <= limit and busy(ink[y0:y1, nx:min(W, nx + band_x)]):
            nx += 1; g += 1
        if g <= limit:
            x1 = nx
        # left
        g, nx = 0, x0
        while nx > 0 and g <= limit and busy(ink[y0:y1, max(0, nx - band_x):nx]):
            nx -= 1; g += 1
        if g <= limit:
            x0 = nx
        if (x0, y0, x1, y1) == before:
            break
    return (x0, y0, x1, y1)


# ---------------------------------------------------------------- detection
def detect_figures(image_path: Path, extra_boxes: list[tuple[float, float, float, float]] | None = None,
                   max_candidates: int = 20) -> list[Candidate]:
    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    H, W = img.shape
    # analyse at ~half resolution for speed
    s = 1600.0 / max(H, W)
    small = cv2.resize(img, (int(W * s), int(H * s)), interpolation=cv2.INTER_AREA)
    mask = _ink_mask(small)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    comps = [(int(x), int(y), int(x + w), int(y + h), int(a))
             for x, y, w, h, a in stats[1:] if a >= 6]
    if not comps:
        return []
    # glyph height: ignore flat strokes (rules, dimension lines) that would skew the median
    hs = np.array([c[3] - c[1] for c in comps if (c[2] - c[0]) < 3 * (c[3] - c[1])])
    small_hs = hs[(hs >= 4) & (hs <= 40)] if len(hs) else hs
    th = float(np.median(small_hs)) if len(small_hs) else 12.0   # typical glyph height

    big2d, hlines, vlines = [], [], []
    for x0, y0, x1, y1, a in comps:
        w, h = x1 - x0, y1 - y0
        if w > 0.9 * small.shape[1] or h > 0.9 * small.shape[0]:
            continue  # page border / scan edge
        cy = (y0 + y1) / 2
        if w > 0.5 * small.shape[1] and (cy < 0.10 * small.shape[0] or cy > 0.92 * small.shape[0]):
            continue  # running header/footer rule (may be curved on phone scans)
        if a / float(w * h) > 0.6 and w > 1.5 * th and h > 1.5 * th:
            continue  # solid 2-D blob (number badge, binder clip, scan shadow) - drawings are outlines
        if w > 2.4 * th and h > 2.4 * th:
            big2d.append((x0, y0, x1, y1))
        elif (w > 5 * th and h <= 0.6 * th) or (w > 8 * th and h <= 1.1 * th):
            if w > 0.55 * small.shape[1]:
                continue  # running header/footer rule spanning the page
            hlines.append((x0, y0, x1, y1))
        elif h > 4 * th and w <= 0.6 * th:
            vlines.append((x0, y0, x1, y1))
        elif w > 4 * th and h > 0.9 * th and a / float(w * h) < 0.25:
            big2d.append((x0, y0, x1, y1))  # thin wide outline: curve, triangle, rectangle, BMD

    seeds = big2d + hlines + vlines
    regions = []
    for r in _merge_boxes(seeds, pad=int(1.6 * th)):
        for a in _split_on_gaps(mask, r, gap=int(1.2 * th)):
            for b in _split_on_gaps(mask.T, (a[1], a[0], a[3], a[2]), gap=int(2.0 * th)):
                b = (b[1], b[0], b[3], b[2])
                regions.extend(_split_on_gaps(mask, b, gap=int(1.2 * th)))

    kept: list[Box] = []
    leftovers: list[Box] = []
    for r in regions:
        inside_big = sum(1 for b in big2d if _overlap(b, r))
        inside_h = sum(1 for b in hlines if _overlap(b, r))
        inside_v = sum(1 for b in vlines if _overlap(b, r))
        graphic = inside_big >= 1 or (inside_h >= 1 and inside_v >= 1) or (inside_h + inside_v) >= 3
        if not graphic:
            leftovers.append(r)  # lone line/arrow: maybe a dimension line split off a diagram
            continue
        rw, rh = r[2] - r[0], r[3] - r[1]
        if rw < 3 * th or rh < 0.9 * th or rw * rh < 5 * th * th:
            continue  # too small to be a diagram (flat shapes like BMD strips are allowed)
        near_edge = r[0] <= 0.06 * small.shape[1] or r[2] >= 0.94 * small.shape[1]
        if near_edge and ((mask[r[1]:r[3], r[0]:r[2]] > 0).mean() > 0.45
                          or float(small[r[1]:r[3], r[0]:r[2]].mean()) < 120):
            continue  # dark blob at the page edge: binder clip / scan shadow
        if rh < 2.2 * th and any(
                _overlap(c[:4], r) and (c[2] - c[0]) > 1.5 * th and (c[3] - c[1]) > 0.8 * th
                and c[4] / float((c[2] - c[0]) * (c[3] - c[1])) > 0.4 for c in comps):
            continue  # question/solution number badge like "2.17 (c)"
        # grow to absorb small labels ("20 kN", "A", "L/2") hugging the drawing
        # absorb against the ORIGINAL region only (no cascading into paragraphs)
        core, grown = r, r
        for c in comps:
            cb, a = c[:4], c[4]
            w, h = cb[2] - cb[0], cb[3] - cb[1]
            if h > 2.5 * th or w > 4 * th or (w * h > 0 and a / (w * h) > 0.55 and w > 1.5 * th):
                continue  # tall glyphs, words, or filled badges like the question-number box
            if _overlap(cb, core, pad=int(0.8 * th)):
                grown = _union(grown, cb)
        r = _trim_text_edges(mask, _grow_to_whitespace(mask, grown, th), th)
        if _area(r) > 0.6 * small.shape[0] * small.shape[1]:
            continue
        kept.append(r)

    # re-attach split-off dimension lines / arrows sitting just above or below a diagram
    for lo in leftovers:
        best, best_gap = None, 2.5 * th
        for i, k in enumerate(kept):
            h_overlap = min(lo[2], k[2]) - max(lo[0], k[0])
            v_gap = max(lo[1] - k[3], k[1] - lo[3])
            if h_overlap > 0.5 * (lo[2] - lo[0]) and v_gap <= best_gap:
                best, best_gap = i, v_gap
        if best is not None:
            kept[best] = _trim_text_edges(mask, _grow_to_whitespace(mask, _union(kept[best], lo), th), th)

    # scale back to render pixels
    inv = 1.0 / s
    boxes = [(int(b[0] * inv), int(b[1] * inv), int(b[2] * inv), int(b[3] * inv)) for b in kept]
    for eb in extra_boxes or []:
        boxes.append(tuple(int(v) for v in eb))  # type: ignore[arg-type]
    boxes = _merge_boxes(boxes, pad=int(0.4 * th * inv))
    boxes = [_pad(b, int(0.9 * th * inv), W, H) for b in boxes]

    # reading order: column (left/right half) then top-down
    mid = W / 2
    boxes.sort(key=lambda b: (0 if (b[0] + b[2]) / 2 < mid else 1, b[1]))
    boxes = sorted(boxes, key=_area, reverse=True)[:max_candidates]
    boxes.sort(key=lambda b: (0 if (b[0] + b[2]) / 2 < mid else 1, b[1]))
    return [Candidate(f"F{i + 1}", b) for i, b in enumerate(boxes)]


def _pad(b: Box, p: int, W: int, H: int) -> Box:
    return (max(0, b[0] - p), max(0, b[1] - p), min(W, b[2] + p), min(H, b[3] + p))


def refine_box(image_path: Path, norm_box: tuple[float, float, float, float]) -> Box | None:
    """Snap an approximate 0-1000 normalised box from the LLM onto real ink."""
    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    H, W = img.shape
    x0, y0, x1, y1 = (int(norm_box[0] / 1000 * W), int(norm_box[1] / 1000 * H),
                      int(norm_box[2] / 1000 * W), int(norm_box[3] / 1000 * H))
    if x1 <= x0 or y1 <= y0:
        return None
    mx, my = int(0.03 * W), int(0.02 * H)
    X0, Y0, X1, Y1 = max(0, x0 - mx), max(0, y0 - my), min(W, x1 + mx), min(H, y1 + my)
    roi = _ink_mask(img[Y0:Y1, X0:X1])
    n, _, stats, _ = cv2.connectedComponentsWithStats(roi, connectivity=8)
    core = (x0 - X0, y0 - Y0, x1 - X0, y1 - Y0)
    acc: Box | None = None
    for x, y, w, h, a in stats[1:]:
        if a < 6:
            continue
        cb = (int(x), int(y), int(x + w), int(y + h))
        if _overlap(cb, core):
            acc = cb if acc is None else _union(acc, cb)
    if acc is None:
        return (x0, y0, x1, y1)
    full = _ink_mask(img)
    th = 0.009 * H  # ~glyph height on an A4 page render
    grown = _grow_to_whitespace(full, (acc[0] + X0, acc[1] + Y0, acc[2] + X0, acc[3] + Y0), th)
    return _pad(grown, int(0.5 * th), W, H)


# ---------------------------------------------------------------- rendering
_COLORS = [(220, 30, 30), (20, 120, 220), (0, 150, 60), (200, 0, 200), (230, 120, 0), (0, 160, 160)]


def annotate_for_llm(image_path: Path, candidates: list[Candidate], out_path: Path,
                     long_edge: int = 1600) -> Path:
    """Downscaled JPEG with thin labelled boxes, sent to the model."""
    img = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    H, W = img.shape[:2]
    s = long_edge / max(H, W)
    img = cv2.resize(img, (int(W * s), int(H * s)), interpolation=cv2.INTER_AREA)
    for i, c in enumerate(candidates):
        col = _COLORS[i % len(_COLORS)][::-1]  # BGR
        x0, y0, x1, y1 = (int(v * s) for v in c.box)
        cv2.rectangle(img, (x0, y0), (x1, y1), col, 2)
        label = c.label
        (tw, tht), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        ty = y0 - 4 if y0 - tht - 6 > 0 else y0 + tht + 4
        cv2.rectangle(img, (x0, ty - tht - 3), (x0 + tw + 6, ty + 3), col, -1)
        cv2.putText(img, label, (x0 + 3, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return out_path


def crop(image_path: Path, box: Box, out_path: Path) -> Path:
    img = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    x0, y0, x1, y1 = box
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), img[y0:y1, x0:x1])
    return out_path
