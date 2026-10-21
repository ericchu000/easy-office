"""Semantic chunk construction for office documents.

Chunks are built around the document's heading tree, one physical page at a
time, so every text chunk has an exact page and a tight bounding box. Tables,
pictures and formulas become independent chunks with a section/page prefix and
their structured original payload.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from easy_office.config.models import LocalParserConfig
from easy_office.indexing.base import ChunkInput
from easy_office.parsing.models import (
    BBox,
    ParsedBlock,
    ParsedDocument,
    ParsedFormula,
    ParsedPicture,
    ParsedTable,
    bbox_union,
)
from easy_office.parsing.tables import render_table


@dataclass
class _Line:
    text: str
    page_no: int | None
    bbox: BBox | None


@dataclass
class _Buffer:
    lines: list[_Line] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(len(line.text) for line in self.lines)

    @property
    def page_no(self) -> int | None:
        for line in self.lines:
            if line.page_no is not None:
                return line.page_no
        return None

    def clear(self) -> None:
        self.lines.clear()


class OfficeChunker:
    """Turns a :class:`ParsedDocument` into ordered ``ChunkInput`` objects."""

    def __init__(self, config: LocalParserConfig) -> None:
        self._max_chars = int(config.max_chunk_chars)

    def build(self, document: ParsedDocument) -> list[ChunkInput]:
        chunks: list[ChunkInput] = []
        heading_stack: list[str] = []
        buffer = _Buffer()
        order = 0

        def flush() -> None:
            nonlocal order
            if not buffer.lines:
                return
            content = "\n".join(line.text for line in buffer.lines if line.text.strip())
            if not content.strip():
                buffer.clear()
                return
            page_no = buffer.page_no
            pages = {line.page_no for line in buffer.lines if line.page_no is not None}
            box = (
                bbox_union(line.bbox for line in buffer.lines)
                if len(pages) <= 1
                else None
            )
            head = self._prefix(heading_stack, page_no, document)
            plain = f"{head}\n{content}" if head else content
            chunks.append(
                ChunkInput(
                    chunk_order=order,
                    plain_text=plain,
                    structured_text=content,
                    chunk_type="text",
                    page_start=page_no,
                    page_end=page_no,
                    bbox=box,
                    metadata={
                        "section": list(heading_stack),
                        "page_semantics": document.page_semantics,
                        "source_format": document.source_format,
                        "parser_backend": document.parser_backend,
                    },
                )
            )
            order += 1
            buffer.clear()

        for block in document.blocks:
            if block.kind == "heading":
                flush()
                level = max(1, int(block.level or 1))
                heading_stack = heading_stack[: level - 1] + [block.text.strip()]
                # The heading text lives in the breadcrumb prefix of every
                # following chunk; keeping it out of the body avoids a
                # standalone heading-only chunk and duplicated text.
                continue

            if block.table is not None:
                flush()
                chunk = self._table_chunk(block.table, block, tuple(heading_stack), order, document)
                if chunk is not None:
                    chunks.append(chunk)
                    order += 1
                continue
            if block.picture is not None:
                flush()
                chunk = self._picture_chunk(block.picture, block, tuple(heading_stack), order, document)
                if chunk is not None:
                    chunks.append(chunk)
                    order += 1
                continue
            if block.formula is not None:
                flush()
                chunk = self._formula_chunk(block.formula, block, tuple(heading_stack), order, document)
                if chunk is not None:
                    chunks.append(chunk)
                    order += 1
                continue

            if not block.text.strip():
                continue
            if buffer.lines and (
                buffer.page_no != block.page_no or buffer.size + len(block.text) > self._max_chars
            ):
                flush()
            text = block.text.strip()
            if block.kind == "list" and not re.match(r"^\s*(?:[-*•]|\d+[.)、])\s+", text):
                text = f"- {text}"
            buffer.lines.append(_Line(text=text, page_no=block.page_no, bbox=block.bbox))
            if buffer.size >= self._max_chars:
                flush()

        flush()
        return chunks

    # --------------------------------------------------------------- builders
    def _table_chunk(
        self,
        table: ParsedTable,
        block: ParsedBlock,
        section: tuple[str, ...],
        order: int,
        document: ParsedDocument,
    ) -> ChunkInput | None:
        render = render_table(table)
        if not render.plain_text.strip() and not render.structured_text.strip():
            return None
        head = self._prefix(section, block.page_no, document)
        body_parts: list[str] = []
        if table.caption:
            body_parts.append(table.caption)
        body_parts.append(render.plain_text)
        body = "\n".join(part for part in body_parts if part.strip())
        plain = f"{head}\n{body}" if head else body
        return ChunkInput(
            chunk_order=order,
            plain_text=plain,
            structured_text=render.structured_text,
            chunk_type="table",
            page_start=block.page_no,
            page_end=block.page_no,
            bbox=block.bbox,
            metadata={
                "section": list(section),
                "page_semantics": document.page_semantics,
                "source_format": document.source_format,
                "parser_backend": document.parser_backend,
                "table_format": render.format,
                "table_complexity": render.reasons,
                "table_rows": table.num_rows,
                "table_cols": table.num_cols,
                "caption": table.caption,
            },
        )

    def _picture_chunk(
        self,
        picture: ParsedPicture,
        block: ParsedBlock,
        section: tuple[str, ...],
        order: int,
        document: ParsedDocument,
    ) -> ChunkInput | None:
        if not picture.kept:
            return None
        parts: list[str] = []
        if picture.caption:
            parts.append(f"图注：{picture.caption}")
        context = " ".join(
            part for part in (picture.context_before, picture.context_after) if part
        ).strip()
        if context:
            parts.append(f"上下文：{context[:800]}")
        if picture.description:
            parts.append(f"图片描述：{picture.description}")
        if picture.ocr_text:
            parts.append(f"图中文字：{picture.ocr_text}")
        body = "\n".join(parts).strip()
        if not body:
            body = f"图片（{picture.width}×{picture.height} 像素）"
        head = self._prefix(section, block.page_no, document)
        plain = f"{head}\n{body}" if head else body
        return ChunkInput(
            chunk_order=order,
            plain_text=plain,
            structured_text=body,
            chunk_type="image",
            page_start=block.page_no,
            page_end=block.page_no,
            bbox=block.bbox,
            image_path=picture.asset_path,
            metadata={
                "section": list(section),
                "page_semantics": document.page_semantics,
                "source_format": document.source_format,
                "parser_backend": document.parser_backend,
                "caption": picture.caption,
                "description_source": picture.description_source,
                "visual_tags": list(picture.visual_tags),
                "image_width": picture.width,
                "image_height": picture.height,
                "ocr_used": bool(picture.ocr_text),
            },
        )

    def _formula_chunk(
        self,
        formula: ParsedFormula,
        block: ParsedBlock,
        section: tuple[str, ...],
        order: int,
        document: ParsedDocument,
    ) -> ChunkInput | None:
        text = formula.display_text
        if not text:
            return None
        head = self._prefix(section, block.page_no, document)
        plain = f"{head}\n{text}" if head else text
        return ChunkInput(
            chunk_order=order,
            plain_text=plain,
            structured_text=text,
            chunk_type="formula",
            page_start=block.page_no,
            page_end=block.page_no,
            bbox=block.bbox,
            metadata={
                "section": list(section),
                "page_semantics": document.page_semantics,
                "source_format": document.source_format,
                "parser_backend": document.parser_backend,
                "latex": formula.latex,
            },
        )

    @staticmethod
    def _prefix(
        section: tuple[str, ...], page_no: int | None, document: ParsedDocument
    ) -> str:
        parts: list[str] = []
        if section:
            parts.append("章节: " + " > ".join(section))
        if page_no is not None and document.page_semantics != "flow":
            if document.page_semantics == "slide":
                parts.append(f"第 {page_no} 张幻灯片")
            else:
                parts.append(f"第 {page_no} 页")
        return f"[{' | '.join(parts)}]" if parts else ""
