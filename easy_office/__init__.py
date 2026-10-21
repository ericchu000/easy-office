"""Easy Office: local multimodal intelligent office assistant.

Implemented so far: the Step 1 foundation (configuration, dual-mode storage,
layered metadata, hybrid index and mirror cache) and the Step 2 office
document parsing pipeline (Docling, LibreOffice legacy conversion, PDF page
routing, table/image processing, chunks and structure summaries).
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
