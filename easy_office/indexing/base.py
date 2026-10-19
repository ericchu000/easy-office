"""Chunk value objects and the pluggable hybrid index contract."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from easy_office.metadata.models import FileQuery, FileRecord
from easy_office.utils import json_loads


@dataclass
class ChunkInput:
    """One retrieval unit produced by a parsing pipeline."""

    chunk_order: int
    plain_text: str = ""
    structured_text: str = ""
    chunk_type: str = "text"
    page_start: int | None = None
    page_end: int | None = None
    sheet_name: str | None = None
    row_start: int | None = None
    row_end: int | None = None
    col_start: int | None = None
    col_end: int | None = None
    bbox: Sequence[float] | None = None
    image_path: str | None = None
    embedding: Sequence[float] | None = None
    metadata: Mapping[str, Any] | None = None
    chunk_uid: str | None = None

    def __post_init__(self) -> None:
        if int(self.chunk_order) < 0:
            raise ValueError("chunk_order must be non-negative")
        self.chunk_order = int(self.chunk_order)
        self.plain_text = str(self.plain_text or "")
        self.structured_text = str(self.structured_text or "")
        if not self.plain_text.strip() and not self.structured_text.strip():
            raise ValueError("chunk must contain plain_text or structured_text")
        for name in (
            "page_start",
            "page_end",
            "row_start",
            "row_end",
            "col_start",
            "col_end",
        ):
            value = getattr(self, name)
            if value is not None:
                if int(value) < 0:
                    raise ValueError(f"{name} must be non-negative")
                setattr(self, name, int(value))
        for start_name, end_name in (
            ("page_start", "page_end"),
            ("row_start", "row_end"),
            ("col_start", "col_end"),
        ):
            start = getattr(self, start_name)
            end = getattr(self, end_name)
            if start is not None and end is not None and start > end:
                raise ValueError(f"{start_name} must not exceed {end_name}")
        if self.bbox is not None:
            if len(self.bbox) != 4:
                raise ValueError("bbox must contain [x0, y0, x1, y1]")
            self.bbox = tuple(float(value) for value in self.bbox)
        if self.embedding is not None:
            self.embedding = [float(value) for value in self.embedding]
        if self.metadata is not None:
            self.metadata = dict(self.metadata)


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: int
    chunk_uid: str
    file_uid: str
    chunk_order: int
    chunk_type: str
    page_start: int | None
    page_end: int | None
    sheet_name: str | None
    row_start: int | None
    row_end: int | None
    col_start: int | None
    col_end: int | None
    bbox: tuple[float, ...] | None
    image_path: str | None
    structured_text: str
    plain_text: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ChunkRecord":
        raw_bbox = json_loads(row["bbox"], None)
        return cls(
            chunk_id=int(row["chunk_id"]),
            chunk_uid=row["chunk_uid"],
            file_uid=row["file_uid"],
            chunk_order=int(row["chunk_order"]),
            chunk_type=row["chunk_type"],
            page_start=row["page_start"],
            page_end=row["page_end"],
            sheet_name=row["sheet_name"],
            row_start=row["row_start"],
            row_end=row["row_end"],
            col_start=row["col_start"],
            col_end=row["col_end"],
            bbox=tuple(raw_bbox) if isinstance(raw_bbox, list) else None,
            image_path=row["image_path"],
            structured_text=row["structured_text"],
            plain_text=row["plain_text"],
            metadata=dict(json_loads(row["metadata"], {}) or {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "chunk_uid": self.chunk_uid,
            "file_uid": self.file_uid,
            "chunk_order": self.chunk_order,
            "chunk_type": self.chunk_type,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "sheet_name": self.sheet_name,
            "row_start": self.row_start,
            "row_end": self.row_end,
            "col_start": self.col_start,
            "col_end": self.col_end,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "image_path": self.image_path,
            "structured_text": self.structured_text,
            "plain_text": self.plain_text,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class SearchHit:
    """A ranked chunk plus the file metadata needed for traceability."""

    chunk: ChunkRecord
    file: FileRecord
    score: float
    rank: int
    snippet: str
    source: str = field(default="hybrid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk": self.chunk.to_dict(),
            "file": self.file.to_dict(),
            "score": self.score,
            "rank": self.rank,
            "snippet": self.snippet,
            "source": self.source,
        }


class HybridIndexStorage(ABC):
    """Contract for full-text + dense-vector chunk storage."""

    @property
    @abstractmethod
    def embedding_dim(self) -> int:
        """Dimension accepted by the vector index."""

    @abstractmethod
    def upsert_chunks(self, file_uid: str, chunks: Sequence[ChunkInput]) -> list[ChunkRecord]:
        """Insert or replace chunks keyed by ``(file_uid, chunk_order)``."""

    @abstractmethod
    def replace_file_chunks(
        self, file_uid: str, chunks: Sequence[ChunkInput]
    ) -> list[ChunkRecord]:
        """Delete all chunks for a file, then insert the supplied set."""

    @abstractmethod
    def delete_chunks_for_file(self, file_uid: str) -> int:
        """Remove every chunk, FTS row, vector and mirror entry for a file."""

    @abstractmethod
    def count_chunks(self, file_uid: str | None = None) -> int:
        """Count indexed chunks, optionally scoped to one file."""

    @abstractmethod
    def search_bm25(
        self,
        query: str,
        *,
        file_filter: FileQuery | None = None,
        top_k: int = 10,
        operator: str = "OR",
    ) -> list[SearchHit]:
        """Rank chunks with SQLite FTS5 BM25."""

    @abstractmethod
    def search_vector(
        self,
        query_vector: Sequence[float],
        *,
        file_filter: FileQuery | None = None,
        top_k: int = 10,
    ) -> list[SearchHit]:
        """Rank chunks by cosine similarity against a dense query vector."""

    @abstractmethod
    def get_chunks(
        self,
        file_uid: str,
        *,
        page_start: int | None = None,
        page_end: int | None = None,
        limit: int | None = None,
    ) -> list[ChunkRecord]:
        """Read chunks in document order, optionally restricted to a page range."""

    @abstractmethod
    def get_chunk(self, chunk_id: int) -> ChunkRecord | None:
        """Read a single chunk."""
