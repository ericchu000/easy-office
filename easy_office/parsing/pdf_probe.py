"""PyMuPDF-based page probing and PDF subset splitting.

The probe is deliberately cheap: it inspects the text layer provided by the PDF
itself and the area covered by raster images. It never runs OCR. The result is
used to route whole PDFs (or page subsets) to the fast digital channel or the
OCR channel.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from easy_office.parsing.models import PageProbe
from easy_office.parsing.text_signals import looks_like_formula

_WHITESPACE = re.compile(r"\s+")


class PdfProbeError(RuntimeError):
    """Raised when PyMuPDF cannot open or inspect a PDF."""


def _import_fitz():
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        try:
            import fitz  # type: ignore
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise PdfProbeError(
                "PyMuPDF is required for PDF page probing. Install it with: "
                "uv add pymupdf"
            ) from exc
    return fitz


def _image_coverage(fitz, page) -> float:
    page_rect = page.rect
    area = abs(float(page_rect.width) * float(page_rect.height))
    if area <= 0:
        return 0.0
    covered = 0.0
    seen: set[int] = set()
    for image in page.get_images(full=True):
        xref = int(image[0])
        if xref in seen:
            continue
        seen.add(xref)
        for rect in page.get_image_rects(xref):
            clipped = rect & page_rect
            if not clipped.is_empty:
                covered += abs(float(clipped.width) * float(clipped.height))
    return min(1.0, covered / area)


def probe_pdf(
    path: str | Path,
    *,
    min_chars: int = 30,
    scan_image_area_ratio: float = 0.6,
) -> list[PageProbe]:
    """Classify every page as digital or scanned using only native signals."""
    fitz = _import_fitz()
    source = Path(path)
    try:
        document = fitz.open(source)
    except Exception as exc:  # pragma: no cover - depends on corrupt input
        raise PdfProbeError(f"cannot open PDF {source.name}: {exc}") from exc
    probes: list[PageProbe] = []
    try:
        for index in range(document.page_count):
            page = document.load_page(index)
            text = page.get_text("text") or ""
            compact = _WHITESPACE.sub("", text)
            char_count = len(compact)
            total = max(1, len(text))
            garbled = text.count("�") + sum(
                1 for ch in text if ord(ch) < 32 and ch not in "\n\r\t"
            )
            garbled_ratio = garbled / total
            coverage = _image_coverage(fitz, page)
            scanned = (
                garbled_ratio > 0.2
                or (coverage >= scan_image_area_ratio and char_count < min_chars)
                or (char_count == 0 and coverage >= 0.05)
            )
            probes.append(
                PageProbe(
                    page_no=index + 1,
                    width=float(page.rect.width),
                    height=float(page.rect.height),
                    char_count=char_count,
                    image_coverage=coverage,
                    garbled_ratio=garble_ratio_safe(garbled_ratio),
                    is_scanned=scanned,
                    formula_hint=looks_like_formula(text),
                )
            )
    finally:
        document.close()
    return probes


def garble_ratio_safe(value: float) -> float:
    """Clamp a ratio into ``[0, 1]`` for stable JSON output."""
    return max(0.0, min(1.0, float(value)))


def page_count(path: str | Path) -> int:
    fitz = _import_fitz()
    try:
        with fitz.open(Path(path)) as document:
            return int(document.page_count)
    except Exception as exc:  # pragma: no cover - corrupt input
        raise PdfProbeError(f"cannot read PDF page count: {exc}") from exc


def split_pdf(
    source: str | Path,
    pages: Sequence[int],
    output: str | Path,
) -> Path:
    """Write a PDF containing exactly ``pages`` (1-based), preserving order."""
    fitz = _import_fitz()
    ordered = sorted({int(page) for page in pages})
    if not ordered:
        raise ValueError("at least one page number is required")
    source_path = Path(source)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(source_path) as document:
        invalid = [page for page in ordered if page < 1 or page > document.page_count]
        if invalid:
            raise ValueError(f"page numbers out of range: {invalid}")
        with fitz.open() as target:
            for page_no in ordered:
                target.insert_pdf(document, from_page=page_no - 1, to_page=page_no - 1)
            target.save(output_path)
    return output_path


def render_page_png(
    source: str | Path,
    page_no: int,
    output: str | Path,
    *,
    dpi: int = 200,
) -> Path:
    """Render one page to PNG (used by tests and the scanned-page fallback)."""
    fitz = _import_fitz()
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(Path(source)) as document:
        page = document.load_page(int(page_no) - 1)
        pixmap = page.get_pixmap(dpi=int(dpi), alpha=False)
        pixmap.save(output_path)
    return output_path


def digital_and_scanned_pages(
    probes: Sequence[PageProbe],
) -> tuple[list[int], list[int]]:
    digital = [probe.page_no for probe in probes if not probe.is_scanned]
    scanned = [probe.page_no for probe in probes if probe.is_scanned]
    return digital, scanned
