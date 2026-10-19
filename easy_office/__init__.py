"""Easy Office: local multimodal intelligent office assistant.

This package currently exposes the Step 1 foundation: dynamic configuration,
dual-mode file storage, layered metadata, and a SQLite hybrid index with a
plain-text companion mirror cache.
"""

from easy_office.core import EasyOfficeCore
from easy_office.config.manager import ConfigManager
from easy_office.config.models import AppConfig
from easy_office.indexing.mirror import MirrorCache
from easy_office.indexing.sqlite_store import SQLiteHybridIndexStore
from easy_office.metadata.repository import MetadataStore
from easy_office.storage.local import LocalFileStorage

__all__ = [
    "AppConfig",
    "ConfigManager",
    "EasyOfficeCore",
    "LocalFileStorage",
    "MetadataStore",
    "MirrorCache",
    "SQLiteHybridIndexStore",
]

__version__ = "0.1.0"
