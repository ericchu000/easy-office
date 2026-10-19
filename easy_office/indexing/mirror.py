"""Plain-text companion mirror cache for binary source files.

Every physical line carries a machine-readable prefix so a ripgrep hit can be
mapped back to the source file UID and page/sheet/row location:

    @@EO|<uid>|<kind>|<page_start>|<page_end>|<sheet>|<row_start>|<row_end>|<col_start>|<col_end>|<text>

Empty location fields are blank. Sheet names are percent-encoded to keep the
delimiter unambiguous. The text field is the remainder of the line, so it may
contain ``|`` characters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import quote, unquote

from easy_office.indexing.tokenizer import plain_text_for_index, strip_html
from easy_office.utils import atomic_write_text

_PREFIX = "@@EO"
_SAFE_UID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MIRROR_KINDS = {"text", "table", "image", "summary"}


def _optional_int(value: str) -> int | None:
    return int(value) if value.strip() else None


def _int_field(value: int | None) -> str:
    return "" if value is None else str(int(value))


@dataclass(frozen=True)
class MirrorLine:
    file_uid: str
    kind: str
    text: str
    page_start: int | None = None
    page_end: int | None = None
    sheet_name: str | None = None
    row_start: int | None = None
    row_end: int | None = None
    col_start: int | None = None
    col_end: int | None = None

    def to_line(self) -> str:
        if not _SAFE_UID.fullmatch(self.file_uid):
            raise ValueError(f"unsafe file UID for mirror line: {self.file_uid!r}")
        kind = self.kind if self.kind in _MIRROR_KINDS else "text"
        sheet = "" if self.sheet_name is None else quote(str(self.sheet_name), safe="")
        text = (self.text or "").replace("\r", " ").replace("\n", " ")
        fields = [
            _PREFIX,
            self.file_uid,
            kind,
            _int_field(self.page_start),
            _int_field(self.page_end),
            sheet,
            _int_field(self.row_start),
            _int_field(self.row_end),
            _int_field(self.col_start),
            _int_field(self.col_end),
            text,
        ]
        return "|".join(fields)

    @classmethod
    def parse(cls, line: str) -> "MirrorLine | None":
        raw = line.rstrip("\r\n")
        if not raw.startswith(_PREFIX + "|"):
            return None
        parts = raw.split("|", 10)
        if len(parts) != 11:
            return None
        _, uid, kind, page_start, page_end, sheet, row_start, row_end, col_start, col_end, text = parts
        if not _SAFE_UID.fullmatch(uid):
            return None
        try:
            return cls(
                file_uid=uid,
                kind=kind if kind in _MIRROR_KINDS else "text",
                text=text,
                page_start=_optional_int(page_start),
                page_end=_optional_int(page_end),
                sheet_name=unquote(sheet) if sheet else None,
                row_start=_optional_int(row_start),
                row_end=_optional_int(row_end),
                col_start=_optional_int(col_start),
                col_end=_optional_int(col_end),
            )
        except ValueError:
            return None


class MirrorCache:
    """Atomic `.txt` mirror files under ``<root>/<uid[:2]>/<uid>.txt``."""

    def __init__(self, root: str | Path, *, enabled: bool = True) -> None:
        self._root = Path(root)
        self._enabled = bool(enabled)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def enabled(self) -> bool:
        return self._enabled

    def reconfigure(self, *, root: str | Path | None = None, enabled: bool | None = None) -> None:
        if root is not None:
            self._root = Path(root)
        if enabled is not None:
            self._enabled = bool(enabled)

    def path_for(self, file_uid: str) -> Path:
        if not _SAFE_UID.fullmatch(file_uid):
            raise ValueError(f"unsafe file UID: {file_uid!r}")
        return self._root / file_uid[:2] / f"{file_uid}.txt"

    def write_lines(self, file_uid: str, lines: Iterable[MirrorLine]) -> Path | None:
        if not self._enabled:
            return None
        materialized = list(lines)
        payload = ""
        if materialized:
            payload = "\n".join(item.to_line() for item in materialized) + "\n"
        path = self.path_for(file_uid)
        atomic_write_text(path, payload)
        return path

    def sync_chunks(self, file_uid: str, chunks: Sequence[Any]) -> Path | None:
        """Rewrite the mirror from chunk-like objects (``ChunkRecord``).

        When mirroring is disabled, any stale mirror for this file is removed so
        ripgrep cannot return content that is no longer maintained.
        """
        if not self._enabled:
            self.delete(file_uid)
            return None
        lines: list[MirrorLine] = []
        for chunk in chunks:
            text = getattr(chunk, "plain_text", "") or strip_html(
                getattr(chunk, "structured_text", "") or ""
            )
            text = plain_text_for_index(text)
            if not text or not text.strip():
                continue
            kind = getattr(chunk, "chunk_type", "text") or "text"
            for physical in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                if not physical.strip():
                    continue
                lines.append(
                    MirrorLine(
                        file_uid=file_uid,
                        kind=kind,
                        text=physical,
                        page_start=getattr(chunk, "page_start", None),
                        page_end=getattr(chunk, "page_end", None),
                        sheet_name=getattr(chunk, "sheet_name", None),
                        row_start=getattr(chunk, "row_start", None),
                        row_end=getattr(chunk, "row_end", None),
                        col_start=getattr(chunk, "col_start", None),
                        col_end=getattr(chunk, "col_end", None),
                    )
                )
        return self.write_lines(file_uid, lines)

    def read_lines(self, file_uid: str) -> list[MirrorLine]:
        path = self.path_for(file_uid)
        if not path.is_file():
            return []
        parsed: list[MirrorLine] = []
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = MirrorLine.parse(raw)
                if line is not None:
                    parsed.append(line)
        return parsed

    def exists(self, file_uid: str) -> bool:
        return self.path_for(file_uid).is_file()

    def delete(self, file_uid: str) -> bool:
        path = self.path_for(file_uid)
        if not path.is_file():
            return False
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return True

    def describe_hit(
        self,
        line: MirrorLine,
        file_lookup: Callable[[str], Any],
    ) -> dict[str, Any]:
        """Map a mirror line back to its source file and precise location."""
        record = file_lookup(line.file_uid)
        return {
            "file_uid": line.file_uid,
            "file_name": getattr(record, "file_name", None),
            "source_path": getattr(record, "local_path", None),
            "storage_mode": getattr(record, "storage_mode", None),
            "mirror_path": str(self.path_for(line.file_uid)),
            "kind": line.kind,
            "page_start": line.page_start,
            "page_end": line.page_end,
            "sheet_name": line.sheet_name,
            "row_start": line.row_start,
            "row_end": line.row_end,
            "col_start": line.col_start,
            "col_end": line.col_end,
            "text": line.text,
        }
