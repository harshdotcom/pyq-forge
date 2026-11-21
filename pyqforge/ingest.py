"""PDF ingestion: page typing (born-digital vs scanned), rendering, text hints."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

RENDER_DPI = 300          # used for figure crops
LLM_LONG_EDGE = 1600      # px; image sent to the model (~2.4K Claude image tokens)


@dataclass
class PageInfo:
    index: int                    # 0-based
    number: int                   # 1-based (what humans and the JSON use)
    is_digital: bool              # has a usable text layer
    width_pt: float
    height_pt: float
    render_path: Path             # 300-DPI PNG
    text_hint: str = ""           # text layer (digital pages only)
    digital_boxes: list[tuple[float, float, float, float]] = field(default_factory=list)  # px @ RENDER_DPI


def parse_page_ranges(spec: str | None, total: int) -> list[int]:
    """'20-40,55' -> sorted 1-based page numbers. None -> all pages."""
    if not spec:
        return list(range(1, total + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
            pages.update(range(max(1, lo), min(total, hi) + 1))
        else:
            n = int(part)
            if 1 <= n <= total:
                pages.add(n)
    return sorted(pages)


def _page_is_digital(page: pymupdf.Page) -> bool:
    text = page.get_text("text").strip()
    if len(text) < 40:
        return False
    # A scanned page with an invisible OCR layer usually has one image covering the page.
    area = page.rect.width * page.rect.height
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        if (x1 - x0) * (y1 - y0) > 0.8 * area:
            # OCR'd scan: text exists but the visual is a picture. Treat as scan, keep hint.
            return False
    return True


def _digital_graphic_boxes(page: pymupdf.Page, scale: float) -> list[tuple[float, float, float, float]]:
    """Embedded images + clustered vector drawings, in render-pixel coords."""
    boxes = []
    area = page.rect.width * page.rect.height
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"])
        if r.get_area() < 0.8 * area and r.width > 20 and r.height > 20:
            boxes.append(r)
    try:
        for r in page.cluster_drawings():
            if r.width > 30 and r.height > 30 and r.get_area() < 0.8 * area:
                boxes.append(r)
    except Exception:  # older PyMuPDF / odd drawings
        pass
    return [(r.x0 * scale, r.y0 * scale, r.x1 * scale, r.y1 * scale) for r in boxes]


def load_pages(pdf_path: Path, work_dir: Path, page_numbers: list[int]) -> list[PageInfo]:
    render_dir = work_dir / "renders"
    render_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    scale = RENDER_DPI / 72.0
    out: list[PageInfo] = []
    for n in page_numbers:
        page = doc[n - 1]
        path = render_dir / f"p{n:04d}.png"
        if not path.exists():
            page.get_pixmap(dpi=RENDER_DPI, colorspace=pymupdf.csRGB).save(path)
        digital = _page_is_digital(page)
        hint = page.get_text("text", sort=True).strip()
        out.append(PageInfo(
            index=n - 1, number=n, is_digital=digital,
            width_pt=page.rect.width, height_pt=page.rect.height,
            render_path=path,
            text_hint=hint if len(hint) >= 40 else "",
            digital_boxes=_digital_graphic_boxes(page, scale) if digital else [],
        ))
    doc.close()
    return out


def page_count(pdf_path: Path) -> int:
    with pymupdf.open(pdf_path) as doc:
        return doc.page_count
