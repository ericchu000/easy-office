"""Storage abstraction for the two physical file storage modes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import BinaryIO


class StorageMode(str, Enum):
    """Where the authoritative bytes live."""

    INPLACE = "inplace"  # Host in-place reference: no copy is created.
    MANAGED = "managed"  # Cross-device upload: bytes are archived by the service.


class StorageError(Exception):
    """Base class for storage failures."""


class PathNotAllowedError(StorageError):
    """Raised when a path falls outside the configured allow-list."""


class StorageNotFoundError(StorageError):
    """Raised when the target object does not exist."""


class StorageConflictError(StorageError):
    """Raised when an operation would overwrite an existing object."""


@dataclass(frozen=True)
class StoredObject:
    """Deterministic description of a stored file.

    ``uri`` is driver-specific (``local:/absolute/path`` today) and is what the
    metadata layer persists for later retrieval through the storage driver.
    """

    file_uid: str
    uri: str
    local_path: Path
    storage_mode: StorageMode
    file_name: str
    extension: str
    mime_type: str
    size_bytes: int
    parent_dir: str
    content_hash: str
    fs_created_at: datetime
    fs_modified_at: datetime
    relative_path: str | None = None

    @property
    def fs_created_ts(self) -> float:
        return self.fs_created_at.timestamp()

    @property
    def fs_modified_ts(self) -> float:
        return self.fs_modified_at.timestamp()


class FileStorage(ABC):
    """Contract a storage driver must implement.

    The interface deliberately speaks in URIs rather than raw paths so a future
    MinIO adapter can reuse it; ``locate`` is only guaranteed for local drivers.
    """

    @abstractmethod
    def register_inplace(self, path: str | Path) -> StoredObject:
        """Register an existing host file without copying its bytes."""

    @abstractmethod
    def ingest_stream(
        self,
        file_name: str,
        stream: BinaryIO,
        *,
        fs_created_at: datetime | str | int | float | None = None,
        fs_modified_at: datetime | str | int | float | None = None,
        file_uid: str | None = None,
        relative_path: str | None = None,
    ) -> StoredObject:
        """Stream an uploaded file into the managed archive workspace."""

    @abstractmethod
    def open_stream(self, uri: str) -> BinaryIO:
        """Open a binary read stream for a stored object."""

    @abstractmethod
    def locate(self, uri: str) -> Path:
        """Return the local filesystem path for a stored object."""

    @abstractmethod
    def compute_sha256(self, uri: str) -> str:
        """Compute the SHA-256 content hash of a stored object."""

    @abstractmethod
    def move(self, src_uri: str, dst_uri: str) -> str:
        """Move an object within the same storage mode and return its new URI."""

    @abstractmethod
    def copy(self, src_uri: str, dst_uri: str) -> str:
        """Copy an object within the same storage mode and return its new URI."""

    @abstractmethod
    def exists(self, uri: str) -> bool:
        """Return whether the object exists."""

    @abstractmethod
    def delete(self, uri: str) -> bool:
        """Delete a managed object (host files are never deleted)."""
