"""Deterministic, zero-token table rendering.

The complexity decision mirrors the product plan: a table is rendered as a
minimal HTML ``<table>`` (preserving ``rowspan``/``colspan``) when any of four
structural signals is present, otherwise as compact Markdown. No model is
consulted and the whole decision is a few microseconds over in-memory cells.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from easy_office.parsing.models import ParsedCell, ParsedTable

_LIST_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)、])\s+", re.MULTILINE)


@dataclass(frozen=True)
class TableComplexity:
    has_spans: bool
    multi_level_header: bool
    multiline_cells: bool
    uneven_row_widths: bool

    @property
    def is_complex(self) -> bool:
        return (
            self.has_spans
            or self.multi_level_header
            or self.multiline_cells
            or self.uneven_row_widths
        )

    def reasons(self) -> list[str]:
        values: list[str] = []
        if self.has_spans:
            values.append("merged_cells")
        if self.multi_level_header:
            values.append("multi_level_header")
        if self.multiline_cells:
            values.append("multiline_cell")
        if self.uneven_row_widths:
            values.append("uneven_row_widths")
        return values


@dataclass(frozen=True)
class TableRender:
    format: str  # "html" | "markdown"
    structured_text: str
    plain_text: str
    complexity: TableComplexity

    @property
    def reasons(self) -> list[str]:
        return self.complexity.reasons()


def analyze_table(table: ParsedTable) -> TableComplexity:
    cells = [cell for cell in table.cells if cell.row_start < table.num_rows]
    has_spans = any(cell.row_span > 1 or cell.col_span > 1 for cell in cells)

    header_rows = {cell.row_start for cell in cells if cell.is_header}
    multi_level_header = len(header_rows) >= 2

    multiline_cells = any(
        "\n" in (cell.text or "") or "\r" in (cell.text or "") or _LIST_LINE.search(cell.text or "")
        for cell in cells
    )

    row_widths: set[int] = set()
    for row in range(table.num_rows):
        width = sum(cell.col_span for cell in cells if cell.row_start == row)
        if width:
            row_widths.add(width)
    uneven_row_widths = len(row_widths) > 1 or (
        bool(row_widths) and row_widths != {table.num_cols}
    )
    return TableComplexity(
        has_spans=has_spans,
        multi_level_header=multi_level_header,
        multiline_cells=multiline_cells,
        uneven_row_widths=uneven_row_widths,
    )


def _cell_grid(table: ParsedTable) -> list[list[ParsedCell | None]]:
    """Place each merged cell at every grid position it covers."""
    grid: list[list[ParsedCell | None]] = [
        [None for _ in range(table.num_cols)] for _ in range(table.num_rows)
    ]
    for cell in table.cells:
        for row in range(max(0, cell.row_start), min(table.num_rows, cell.row_end)):
            for col in range(max(0, cell.col_start), min(table.num_cols, cell.col_end)):
                if grid[row][col] is None:
                    grid[row][col] = cell
    return grid


def _flatten_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\r", " ").replace("\n", " ")).strip()


def _header_row_count(table: ParsedTable) -> int:
    """Leading rows that contain column headers (at most one for Markdown)."""
    header_rows = {
        cell.row_start
        for cell in table.cells
        if cell.is_header and cell.row_start < table.num_rows
    }
    count = 0
    while count in header_rows:
        count += 1
    return count


def render_html(table: ParsedTable) -> str:
    grid = _cell_grid(table)
    header_rows = _header_row_count(table)
    lines: list[str] = ["<table>"]
    if header_rows:
        lines.append("<thead>")
    header_closed = not header_rows
    body_started = False
    for row in range(table.num_rows):
        is_header_row = row < header_rows
        if not is_header_row and not header_closed:
            lines.append("</thead>")
            header_closed = True
        if not is_header_row and not body_started:
            lines.append("<tbody>")
            body_started = True
        lines.append("<tr>")
        for col in range(table.num_cols):
            cell = grid[row][col]
            if cell is None:
                lines.append("<td></td>")
                continue
            # Render a merged cell only at its origin.
            if cell.row_start != row or cell.col_start != col:
                continue
            tag = "th" if cell.is_header else "td"
            attrs: list[str] = []
            if cell.row_span > 1:
                attrs.append(f'rowspan="{cell.row_span}"')
            if cell.col_span > 1:
                attrs.append(f'colspan="{cell.col_span}"')
            attr_text = f" {' '.join(attrs)}" if attrs else ""
            text = html.escape(_flatten_text(cell.text), quote=False)
            lines.append(f"<{tag}{attr_text}>{text}</{tag}>")
        lines.append("</tr>")
    if not header_closed:
        lines.append("</thead>")
    if body_started:
        lines.append("</tbody>")
    lines.append("</table>")
    return "".join(lines)


def _md_escape(text: str) -> str:
    return _flatten_text(text).replace("|", "\\|")


def render_markdown(table: ParsedTable) -> str:
    grid = _cell_grid(table)
    rows: list[list[str]] = []
    for row in range(table.num_rows):
        values: list[str] = []
        for col in range(table.num_cols):
            cell = grid[row][col]
            values.append(_md_escape(cell.text) if cell is not None else "")
        rows.append(values)
    if not rows:
        return ""
    width = max(len(row) for row in rows) or table.num_cols or 1
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |"]
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def table_plain_text(table: ParsedTable, *, include_headers: bool = True) -> str:
    """Markup-free text used for FTS, embeddings and the mirror cache."""
    parts: list[str] = []
    caption = _flatten_text(table.caption)
    if caption:
        parts.append(caption)
    headers = table.header_texts()
    if include_headers and headers:
        parts.append("表头: " + " | ".join(headers))
    grid = _cell_grid(table)
    for row in range(table.num_rows):
        values: list[str] = []
        for col in range(table.num_cols):
            cell = grid[row][col]
            if cell is None:
                values.append("")
                continue
            # A merged cell contributes text only once, at its origin.
            if cell.row_start == row and cell.col_start == col:
                values.append(_flatten_text(cell.text))
            else:
                values.append("")
        line = " | ".join(values).strip()
        if line.replace("|", "").strip():
            parts.append(line)
    return "\n".join(parts)


def render_table(table: ParsedTable) -> TableRender:
    """Choose HTML or Markdown and produce both structured and plain text."""
    complexity = analyze_table(table)
    if complexity.is_complex:
        structured = render_html(table)
        fmt = "html"
    else:
        structured = render_markdown(table)
        fmt = "markdown"
    return TableRender(
        format=fmt,
        structured_text=structured,
        plain_text=table_plain_text(table),
        complexity=complexity,
    )


def cells_from_grid(
    grid: list[list[str | None]], *, header_rows: int = 1
) -> list[ParsedCell]:
    """Build single-span cells from a plain string grid (test/CSV helpers)."""
    cells: list[ParsedCell] = []
    for row_index, row in enumerate(grid):
        for col_index, value in enumerate(row):
            if value is None:
                continue
            cells.append(
                ParsedCell(
                    text=str(value),
                    row_start=row_index,
                    row_end=row_index + 1,
                    col_start=col_index,
                    col_end=col_index + 1,
                    is_header=row_index < header_rows,
                )
            )
    return cells
