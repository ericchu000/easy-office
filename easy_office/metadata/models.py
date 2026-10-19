"""Metadata value objects and query filters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from easy_office.storage.base import StorageMode
from easy_office.utils import json_loads


class MetadataError(Exception):
    """Base class for metadata failures."""


class FileRecordNotFound(MetadataError):
    """Raised when a requested file UID is unknown."""


class AmbiguousContentHashError(MetadataError):
    """Raised when one content hash maps to more than one file record."""


class MetadataValidationError(MetadataError):
    """Raised when a semantic metadata update is structurally invalid."""


class ModalitySummaryConflict(MetadataError):
    """Raised when a file is assigned two different modality summaries."""


MODALITY_KINDS = ("office", "image", "table")


@dataclass(frozen=True)
class FileRecord:
    """One row of the layered metadata table, with JSON columns decoded."""

    file_uid: str
    file_name: str
    extension: str
    mime_type: str
    size_bytes: int
    parent_dir: str
    storage_mode: str
    content_hash: str
    uri: str
    local_path: str
    relative_path: str | None
    fs_created_at: str
    fs_modified_at: str
    fs_created_ts: float
    fs_modified_ts: float
    registered_at: str
    updated_at: str
    topic_tags: list[str]
    summary: str | None
    entities: list[str]
    keywords: list[str]
    business_start_date: str | None
    business_end_date: str | None
    modality_summary_kind: str | None
    modality_summary: dict[str, Any] | None
    deleted_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "FileRecord":
        return cls(
            file_uid=row["file_uid"],
            file_name=row["file_name"],
            extension=row["extension"],
            mime_type=row["mime_type"],
            size_bytes=int(row["size_bytes"]),
            parent_dir=row["parent_dir"],
            storage_mode=row["storage_mode"],
            content_hash=row["content_hash"],
            uri=row["uri"],
            local_path=row["local_path"],
            relative_path=row["relative_path"],
            fs_created_at=row["fs_created_at"],
            fs_modified_at=row["fs_modified_at"],
            fs_created_ts=float(row["fs_created_ts"]),
            fs_modified_ts=float(row["fs_modified_ts"]),
            registered_at=row["registered_at"],
            updated_at=row["updated_at"],
            topic_tags=list(json_loads(row["topic_tags"], []) or []),
            summary=row["summary"],
            entities=list(json_loads(row["entities"], []) or []),
            keywords=list(json_loads(row["keywords"], []) or []),
            business_start_date=row["business_start_date"],
            business_end_date=row["business_end_date"],
            modality_summary_kind=row["modality_summary_kind"],
            modality_summary=json_loads(row["modality_summary"], None),
            deleted_at=row["deleted_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_uid": self.file_uid,
            "file_name": self.file_name,
            "extension": self.extension,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "parent_dir": self.parent_dir,
            "storage_mode": self.storage_mode,
            "content_hash": self.content_hash,
            "uri": self.uri,
            "local_path": self.local_path,
            "relative_path": self.relative_path,
            "fs_created_at": self.fs_created_at,
            "fs_modified_at": self.fs_modified_at,
            "fs_created_ts": self.fs_created_ts,
            "fs_modified_ts": self.fs_modified_ts,
            "registered_at": self.registered_at,
            "updated_at": self.updated_at,
            "topic_tags": list(self.topic_tags),
            "summary": self.summary,
            "entities": list(self.entities),
            "keywords": list(self.keywords),
            "business_start_date": self.business_start_date,
            "business_end_date": self.business_end_date,
            "modality_summary_kind": self.modality_summary_kind,
            "modality_summary": self.modality_summary,
        }


_ORDER_COLUMNS = {
    "registered_at": "registered_at",
    "updated_at": "updated_at",
    "fs_created_at": "fs_created_at",
    "fs_modified_at": "fs_modified_at",
    "size_bytes": "size_bytes",
    "file_name": "file_name",
}


@dataclass
class FileQuery:
    """Composable metadata filters used by both the metadata center and search."""

    extensions: Sequence[str] | None = None
    mime_types: Sequence[str] | None = None
    storage_mode: StorageMode | str | None = None
    name_contains: str | None = None
    parent_dir_prefix: str | None = None
    min_size: int | None = None
    max_size: int | None = None
    modified_after: str | datetime | date | None = None
    modified_before: str | datetime | date | None = None
    topic_tags: Sequence[str] | None = None
    require_all_topic_tags: bool = False
    keywords: Sequence[str] | None = None
    entities: Sequence[str] | None = None
    business_start_from: str | date | datetime | None = None
    business_end_to: str | date | datetime | None = None
    modality_kind: str | None = None
    modality_filters: Mapping[str, Any] | None = None
    limit: int | None = None
    offset: int = 0
    order_by: str = "registered_at"
    descending: bool = False

    def normalized_extensions(self) -> list[str]:
        return [str(item).lower().lstrip(".") for item in (self.extensions or []) if str(item)]

    def normalized_modes(self) -> list[str] | None:
        if self.storage_mode is None:
            return None
        try:
            return [StorageMode(self.storage_mode).value]
        except ValueError as exc:
            raise MetadataValidationError(
                f"unsupported storage_mode: {self.storage_mode!r}"
            ) from exc

    def order_column(self) -> str:
        column = _ORDER_COLUMNS.get(self.order_by)
        if column is None:
            raise MetadataValidationError(
                f"unsupported order_by: {self.order_by!r}; "
                f"expected one of {sorted(_ORDER_COLUMNS)}"
            )
        return column
