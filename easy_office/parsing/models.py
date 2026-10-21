"""Backend-neutral value objects for parsed office documents.

A parser driver (Docling today, MinerU/DeepSeek-OCR later) produces only these
objects, so the chunking, summary and indexing layers never depend on a
specific parser's internal types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

BBox = tuple[float, float, float, float]
"""``[x0, y0, x1, y1]`` in page points with a top-left origin."""

PARSER_BACKENDS = ("docling",)
PAGE_SEMANTICS = ("physical", "flow", "slide")


@dataclass
class ParsedCell:
    """One table cell, including its merged-cell span and offsets."""

    text: str
    row_start: int
    row_end: int  # exclusive
    col_start: int
    col_end: int  # exclusive
    is_header: bool = False
    is_row_header: bool = False

    @property
    def row_span(self) -> int:
        return max(1, self.row_end - self.row_start)

    @property
    def col_span(self) -> int:
        return max(1, self.col_end - self.col_start)


@dataclass
class ParsedTable:
    cells: list[ParsedCell]
    num_rows: int
    num_cols: int
    page_no: int | None = None
    bbox: BBox | None = None
    caption: str = ""
    order: int = 0

    @property
    def is_empty(self) -> bool:
        return self.num_rows <= 0 or self.num_cols <= 0 or not self.cells

    def header_texts(self) -> list[str]:
        """Return header-label texts in reading order (deduplicated)."""
        headers: list[str] = []
        for cell in sorted(self.cells, key=lambda c: (c.row_start, c.col_start)):
            if cell.is_header and cell.text.strip() and cell.text.strip() not in headers:
                headers.append(cell.text.strip())
        return headers


@dataclass
class ParsedPicture:
    width: int
    height: int
    image_bytes: bytes | None = None
    image_format: str = "png"
    page_no: int | None = None
    bbox: BBox | None = None
    caption: str = ""
    context_before: str = ""
    context_after: str = ""
    order: int = 0
    # Populated by the post-processing pipeline.
    asset_path: str | None = None
    ocr_text: str = ""
    description: str = ""
    description_source: str = ""
    visual_tags: list[str] = field(default_factory=list)
    filtered_reason: str | None = None

    @property
    def kept(self) -> bool:
        return self.filtered_reason is None


@dataclass
class ParsedFormula:
    text: str
    latex: str | None = None
    page_no: int | None = None
    bbox: BBox | None = None
    order: int = 0

    @property
    def display_text(self) -> str:
        return (self.latex or self.text or "").strip()


@dataclass
class ParsedBlock:
    """One item in reading order.

    Exactly one of ``table``, ``picture`` or ``formula`` is set for special
    blocks; plain text and headings only use ``text``.
    """

    kind: str  # heading | text | list | table | picture | formula | code
    text: str = ""
    level: int = 0
    page_no: int | None = None
    bbox: BBox | None = None
    order: int = 0
    section: tuple[str, ...] = ()
    table: ParsedTable | None = None
    picture: ParsedPicture | None = None
    formula: ParsedFormula | None = None


@dataclass
class PageProbe:
    """Deterministic page classification used to route PDF pages."""

    page_no: int
    width: float = 0.0
    height: float = 0.0
    char_count: int = 0
    image_coverage: float = 0.0
    garbled_ratio: float = 0.0
    is_scanned: bool = False
    formula_hint: bool = False


@dataclass
class ParsedDocument:
    """The full parse result plus provenance needed by later stages."""

    source_path: str
    source_format: str
    parser_backend: str
    blocks: list[ParsedBlock]
    pages: list[PageProbe] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    page_semantics: str = "physical"
    converted_pdf_path: str | None = None
    page_count: int = 0

    @property
    def tables(self) -> list[ParsedTable]:
        return [block.table for block in self.blocks if block.table is not None]

    @property
    def pictures(self) -> list[ParsedPicture]:
        return [block.picture for block in self.blocks if block.picture is not None]

    @property
    def formulas(self) -> list[ParsedFormula]:
        return [block.formula for block in self.blocks if block.formula is not None]

    def iter_characters(self) -> Iterable[str]:
        """Yield the document's original textual content (no generated text)."""
        for block in self.blocks:
            if block.table is not None:
                for cell in block.table.cells:
                    yield cell.text or ""
            elif block.picture is not None:
                yield block.picture.ocr_text or ""
                yield block.picture.caption or ""
            elif block.formula is not None:
                yield block.formula.text or ""
            else:
                yield block.text or ""

    def textual_char_count(self) -> int:
        return sum(
            len("".join(ch for ch in text if not ch.isspace()))
            for text in self.iter_characters()
        )

    def text_sample(self, limit: int = 200_000) -> str:
        parts: list[str] = []
        size = 0
        for block in self.blocks:
            if block.table is not None:
                text = " ".join(cell.text for cell in block.table.cells if cell.text)
            elif block.picture is not None:
                text = f"{block.picture.caption} {block.picture.ocr_text}"
            elif block.formula is not None:
                text = block.formula.display_text
            else:
                text = block.text
            if not text:
                continue
            parts.append(text)
            size += len(text)
            if size >= limit:
                break
        return "\n".join(parts)[:limit]


def bbox_union(boxes: Iterable[BBox | None]) -> BBox | None:
    """Union of boxes on one page; ``None`` whenever any box is unknown."""
    materialized = list(boxes)
    if not materialized or any(box is None for box in materialized):
        return None
    xs0 = min(box[0] for box in materialized if box is not None)
    ys0 = min(box[1] for box in materialized if box is not None)
    xs1 = max(box[2] for box in materialized if box is not None)
    ys1 = max(box[3] for box in materialized if box is not None)
    return (xs0, ys0, xs1, ys1)


def block_to_summary(block: ParsedBlock) -> dict[str, Any]:
    """Small debug representation used in ingest warnings/tests."""
    return {
        "kind": block.kind,
        "page_no": block.page_no,
        "bbox": list(block.bbox) if block.bbox else None,
        "chars": len(block.text or ""),
    }
