"""Dual-mode file object storage."""

from easy_office.storage.base import (
    FileStorage,
    PathNotAllowedError,
    StorageConflictError,
    StorageError,
    StorageMode,
    StorageNotFoundError,
    StoredObject,
)
from easy_office.storage.local import LocalFileStorage

__all__ = [
    "FileStorage",
    "LocalFileStorage",
    "PathNotAllowedError",
    "StorageConflictError",
    "StorageError",
    "StorageMode",
    "StorageNotFoundError",
    "StoredObject",
]
