"""Composition root for the Step 1 foundation services."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import BinaryIO, Sequence

from easy_office.config.manager import ConfigError, ConfigManager
from easy_office.config.models import AppConfig
from easy_office.db.database import Database
from easy_office.indexing.base import ChunkInput, ChunkRecord, SearchHit
from easy_office.indexing.mirror import MirrorCache
from easy_office.indexing.sqlite_store import SQLiteHybridIndexStore
from easy_office.metadata.models import FileQuery, FileRecord
from easy_office.metadata.repository import MetadataStore
from easy_office.storage.base import StorageMode, StorageError
from easy_office.storage.local import LocalFileStorage

logger = logging.getLogger(__name__)


class CoreError(Exception):
    """Base class for composition-level failures."""


class ContentChangedError(StorageError):
    """Raised when a moved file's content hash no longer matches the record."""


class EasyOfficeCore:
    """Wires configuration, storage, metadata and the hybrid index together.

    Later steps build their parsers and agents on top of this object. It is also
    what the Step 8 daemon will wrap in network interfaces.
    """

    def __init__(
        self,
        config: ConfigManager,
        storage: LocalFileStorage,
        db: Database,
        metadata: MetadataStore,
        index: SQLiteHybridIndexStore,
        mirror: MirrorCache,
    ) -> None:
        self._config = config
        self._storage = storage
        self._db = db
        self._metadata = metadata
        self._index = index
        self._mirror = mirror
        self._closed = False
        self._unsubscribe_config = config.subscribe(self._apply_config_change)
        self._remove_config_validator = config.add_validator(self._validate_config_change)

    # -------------------------------------------------------------- bootstrap
    @classmethod
    def bootstrap(
        cls,
        config_path: str | Path | None = None,
        data_dir: str | Path | None = None,
        *,
        config_patch: dict | None = None,
        create_if_missing: bool = True,
    ) -> "EasyOfficeCore":
        manager = ConfigManager(
            config_path=config_path,
            data_dir=data_dir,
            create_if_missing=create_if_missing,
        )
        if config_patch:
            manager.update(config_patch)
        paths = manager.ensure_directories()
        config = manager.get()
        cls._assert_supported(config)

        db = Database(paths.index_db_path, embedding_dim=config.embedding.embedding_dim)
        metadata = MetadataStore(db)
        mirror = MirrorCache(paths.mirror_dir, enabled=config.storage.mirror_enabled)
        index = SQLiteHybridIndexStore(db, metadata, mirror)
        storage = LocalFileStorage(
            paths.managed_workspace,
            paths.host_allowed_roots,
            max_upload_bytes=config.storage.max_upload_bytes,
        )
        return cls(manager, storage, db, metadata, index, mirror)

    # -------------------------------------------------------------- properties
    @property
    def config(self) -> ConfigManager:
        return self._config

    @property
    def storage(self) -> LocalFileStorage:
        return self._storage

    @property
    def db(self) -> Database:
        return self._db

    @property
    def metadata(self) -> MetadataStore:
        return self._metadata

    @property
    def index(self) -> SQLiteHybridIndexStore:
        return self._index

    @property
    def mirror(self) -> MirrorCache:
        return self._mirror

    # --------------------------------------------------------------- ingestion
    def register_inplace(self, path: str | Path) -> FileRecord:
        """Register a host file without copying it into the managed workspace.

        If the content hash changed since the last registration, derived chunks
        and the mirror are cleared so retrieval cannot return stale text before
        the indexing pipeline re-processes the file.
        """
        obj = self._storage.register_inplace(path)
        existing = self._metadata.get_file_by_path(obj.storage_mode, obj.local_path)
        record = self._metadata.register_file(obj)
        if existing is not None and existing.content_hash != obj.content_hash:
            self._index.delete_chunks_for_file(record.file_uid)
        return record

    def ingest_upload(
        self,
        file_name: str,
        stream: BinaryIO,
        *,
        fs_created_at=None,
        fs_modified_at=None,
        relative_path: str | None = None,
    ) -> FileRecord:
        """Stream an uploaded file into the managed workspace and register it."""
        obj = self._storage.ingest_stream(
            file_name,
            stream,
            fs_created_at=fs_created_at,
            fs_modified_at=fs_modified_at,
            relative_path=relative_path,
        )
        return self._metadata.register_file(obj)

    def sync_moved_path(self, file_uid: str, new_path: str | Path) -> FileRecord:
        """Synchronize metadata after an external move or rename.

        The content hash must match the record; otherwise the caller should
        re-index the file instead of updating its path.
        """
        record = self._metadata.get_file(file_uid)
        if record is None:
            raise CoreError(f"unknown file: {file_uid}")
        obj = self._storage.describe_existing(
            new_path, record.storage_mode, file_uid=file_uid
        )
        if obj.content_hash != record.content_hash:
            raise ContentChangedError(
                f"content hash changed for {file_uid}; re-index instead of path sync"
            )
        return self._metadata.update_path(
            file_uid,
            obj.local_path,
            new_uri=obj.uri,
            relative_path=obj.relative_path,
            file_name=obj.file_name,
            extension=obj.extension,
            mime_type=obj.mime_type,
        )

    def move_file(self, file_uid: str, destination: str | Path) -> FileRecord:
        """Physically move a stored file and update its metadata path."""
        record = self._metadata.get_file(file_uid)
        if record is None:
            raise CoreError(f"unknown file: {file_uid}")
        destination_path = Path(destination).expanduser().resolve(strict=False)
        new_uri = self._storage.make_uri(destination_path)
        moved_uri = self._storage.move(
            record.uri, new_uri, mode=record.storage_mode
        )
        moved_path = self._storage.path_from_uri(moved_uri)
        described = self._storage.describe_existing(
            moved_path, record.storage_mode, file_uid=file_uid
        )
        return self._metadata.update_path(
            file_uid,
            moved_path,
            new_uri=moved_uri,
            relative_path=described.relative_path,
            file_name=described.file_name,
            extension=described.extension,
            mime_type=described.mime_type,
        )

    def delete_file(self, file_uid: str, *, delete_managed_bytes: bool = True) -> bool:
        """Delete a file's chunks, mirror, metadata and (if managed) bytes."""
        record = self._metadata.get_file(file_uid)
        if record is None:
            return False
        self._index.delete_chunks_for_file(file_uid)
        if delete_managed_bytes and record.storage_mode == StorageMode.MANAGED.value:
            try:
                self._storage.delete(record.uri)
            except StorageError:
                logger.exception("failed to delete managed bytes for %s", file_uid)
        return self._metadata.delete_file(file_uid)

    # ---------------------------------------------------------------- retrieval
    def add_chunks(self, file_uid: str, chunks: Sequence[ChunkInput]) -> list[ChunkRecord]:
        return self._index.upsert_chunks(file_uid, chunks)

    def search_bm25(
        self,
        query: str,
        *,
        file_filter: FileQuery | None = None,
        top_k: int = 10,
        operator: str = "OR",
    ) -> list[SearchHit]:
        return self._index.search_bm25(
            query, file_filter=file_filter, top_k=top_k, operator=operator
        )

    def search_vector(
        self,
        query_vector: Sequence[float],
        *,
        file_filter: FileQuery | None = None,
        top_k: int = 10,
    ) -> list[SearchHit]:
        return self._index.search_vector(
            query_vector, file_filter=file_filter, top_k=top_k
        )

    # ---------------------------------------------------------------- lifecycle
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._unsubscribe_config()
            self._remove_config_validator()
        except Exception:  # pragma: no cover - defensive
            pass
        self._db.close()

    def __enter__(self) -> "EasyOfficeCore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---------------------------------------------------------------- config
    @staticmethod
    def _assert_supported(config: AppConfig) -> None:
        if config.storage.vector_store_driver != "sqlite":
            raise ConfigError(
                "this build supports only storage.vector_store_driver='sqlite'; "
                f"got {config.storage.vector_store_driver!r}"
            )
        if config.storage.object_store_driver != "local":
            raise ConfigError(
                "this build supports only storage.object_store_driver='local'; "
                f"got {config.storage.object_store_driver!r}"
            )

    def _validate_config_change(self, old: AppConfig, new: AppConfig) -> None:
        self._assert_supported(new)
        if new.embedding.embedding_dim != old.embedding.embedding_dim:
            self._db.assert_dimension_change_allowed(new.embedding.embedding_dim)

    def _apply_config_change(self, new: AppConfig) -> None:
        paths = self._config.paths()
        if paths.index_db_path != self._db.path:
            self._db.reopen(
                paths.index_db_path,
                embedding_dim=new.embedding.embedding_dim,
            )
        elif new.embedding.embedding_dim != self._db.embedding_dim:
            # The validator already ensured no vectors would be lost.
            self._db.set_embedding_dim(
                new.embedding.embedding_dim, allow_rebuild=True
            )

        self._storage.reconfigure(
            managed_workspace=paths.managed_workspace,
            allowed_roots=paths.host_allowed_roots,
            max_upload_bytes=new.storage.max_upload_bytes,
        )
        self._mirror.reconfigure(
            root=paths.mirror_dir,
            enabled=new.storage.mirror_enabled,
        )
