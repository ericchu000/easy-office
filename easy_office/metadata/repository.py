"""SQLite-backed metadata center for layered file metadata."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from easy_office.db.database import Database
from easy_office.metadata.models import (
    MODALITY_KINDS,
    AmbiguousContentHashError,
    FileQuery,
    FileRecord,
    FileRecordNotFound,
    MetadataError,
    MetadataValidationError,
    ModalitySummaryConflict,
)
from easy_office.storage.base import StorageMode, StoredObject
from easy_office.storage.local import detect_mime_type, file_extension
from easy_office.utils import (
    coerce_datetime,
    coerce_iso_date,
    iso_utc,
    json_dumps,
    utc_now,
)

_UNSET = object()
_ALIAS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_JSON_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$")
_MAX_TAGS_PER_KIND = 500
_MAX_TAG_LENGTH = 300


def _normalize_tag_input(values: Any, field: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MetadataValidationError(f"{field} must be a sequence of strings")
    return _normalize_tags(values)


def _normalize_tags(values: Sequence[Any]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if len(text) > _MAX_TAG_LENGTH:
            raise MetadataValidationError(
                f"tag is too long (max {_MAX_TAG_LENGTH} characters): {text[:40]!r}..."
            )
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    if len(cleaned) > _MAX_TAGS_PER_KIND:
        raise MetadataValidationError(
            f"too many tags (max {_MAX_TAGS_PER_KIND}): {len(cleaned)}"
        )
    return cleaned


class MetadataStore:
    """Layered metadata CRUD and compound filtering.

    Layer 1 columns are written by :meth:`register_file`. Layer 2 semantic fields
    and modality summaries are backfilled by later steps through the update
    methods below.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ write
    def register_file(self, obj: StoredObject) -> FileRecord:
        """Insert a new file record or refresh an existing path in place.

        The file UID is preserved when the same absolute path is registered
        again, which keeps references stable across rescans.
        """
        now = iso_utc(utc_now())
        mode = obj.storage_mode.value
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT file_uid FROM files WHERE storage_mode = ? AND local_path = ?",
                (mode, str(obj.local_path)),
            ).fetchone()
            if existing is not None:
                uid = existing["file_uid"]
                try:
                    conn.execute(
                        """
                        UPDATE files SET
                            file_name = ?, extension = ?, mime_type = ?, size_bytes = ?,
                            parent_dir = ?, content_hash = ?, uri = ?, relative_path = ?,
                            fs_created_at = ?, fs_modified_at = ?,
                            fs_created_ts = ?, fs_modified_ts = ?, updated_at = ?
                        WHERE file_uid = ?
                        """,
                        (
                            obj.file_name,
                            obj.extension,
                            obj.mime_type,
                            obj.size_bytes,
                            obj.parent_dir,
                            obj.content_hash,
                            obj.uri,
                            obj.relative_path,
                            iso_utc(obj.fs_created_at),
                            iso_utc(obj.fs_modified_at),
                            obj.fs_created_ts,
                            obj.fs_modified_ts,
                            now,
                            uid,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise MetadataError(
                        f"cannot refresh file {obj.file_name!r}: {exc}"
                    ) from exc
            else:
                uid = obj.file_uid
                try:
                    conn.execute(
                        """
                        INSERT INTO files (
                            file_uid, file_name, extension, mime_type, size_bytes,
                            parent_dir, storage_mode, content_hash, uri, local_path,
                            relative_path, fs_created_at, fs_modified_at,
                            fs_created_ts, fs_modified_ts, registered_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uid,
                            obj.file_name,
                            obj.extension,
                            obj.mime_type,
                            obj.size_bytes,
                            obj.parent_dir,
                            mode,
                            obj.content_hash,
                            obj.uri,
                            str(obj.local_path),
                            obj.relative_path,
                            iso_utc(obj.fs_created_at),
                            iso_utc(obj.fs_modified_at),
                            obj.fs_created_ts,
                            obj.fs_modified_ts,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise MetadataError(
                        f"cannot register file {obj.file_name!r}: {exc}"
                    ) from exc
        record = self.get_file(uid)
        if record is None:  # pragma: no cover - defensive
            raise MetadataError("file record disappeared after registration")
        return record

    def update_semantic_metadata(
        self,
        file_uid: str,
        *,
        topic_tags: Sequence[str] | None | object = _UNSET,
        summary: str | None | object = _UNSET,
        entities: Sequence[str] | None | object = _UNSET,
        keywords: Sequence[str] | None | object = _UNSET,
        business_start_date: Any = _UNSET,
        business_end_date: Any = _UNSET,
    ) -> FileRecord:
        """Backfill layer 2 metadata. ``None`` clears a field; omitted leaves it."""
        current = self.get_file(file_uid)
        if current is None:
            raise FileRecordNotFound(f"file not found: {file_uid}")

        new_topics = (
            current.topic_tags
            if topic_tags is _UNSET
            else _normalize_tag_input(topic_tags, "topic_tags")
        )
        new_entities = (
            current.entities
            if entities is _UNSET
            else _normalize_tag_input(entities, "entities")
        )
        new_keywords = (
            current.keywords
            if keywords is _UNSET
            else _normalize_tag_input(keywords, "keywords")
        )
        new_summary = current.summary if summary is _UNSET else summary
        if new_summary is not None and not isinstance(new_summary, str):
            raise MetadataValidationError("summary must be a string or None")

        try:
            start = (
                current.business_start_date
                if business_start_date is _UNSET
                else coerce_iso_date(business_start_date, field="business_start_date")
            )
            end = (
                current.business_end_date
                if business_end_date is _UNSET
                else coerce_iso_date(business_end_date, field="business_end_date")
            )
        except ValueError as exc:
            raise MetadataValidationError(str(exc)) from exc
        if start and end and start > end:
            raise MetadataValidationError(
                f"business_start_date {start} is after business_end_date {end}"
            )

        now = iso_utc(utc_now())
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE files SET
                    topic_tags = ?, summary = ?, entities = ?, keywords = ?,
                    business_start_date = ?, business_end_date = ?, updated_at = ?
                WHERE file_uid = ?
                """,
                (
                    json_dumps(new_topics),
                    new_summary,
                    json_dumps(new_entities),
                    json_dumps(new_keywords),
                    start,
                    end,
                    now,
                    file_uid,
                ),
            )
            if topic_tags is not _UNSET:
                self._replace_tags(conn, file_uid, "topic", new_topics)
            if entities is not _UNSET:
                self._replace_tags(conn, file_uid, "entity", new_entities)
            if keywords is not _UNSET:
                self._replace_tags(conn, file_uid, "keyword", new_keywords)
        record = self.get_file(file_uid)
        if record is None:  # pragma: no cover - defensive
            raise FileRecordNotFound(f"file not found: {file_uid}")
        return record

    def update_modality_summary(
        self,
        file_uid: str,
        kind: str,
        summary: Mapping[str, Any],
        *,
        extra_tags: Mapping[str, Sequence[str]] | None = None,
    ) -> FileRecord:
        """Attach one modality-specific summary; different kinds are rejected."""
        if kind not in MODALITY_KINDS:
            raise MetadataValidationError(
                f"unsupported modality summary kind: {kind!r}; expected {MODALITY_KINDS}"
            )
        if not isinstance(summary, Mapping):
            raise MetadataValidationError("modality summary must be a mapping")
        current = self.get_file(file_uid)
        if current is None:
            raise FileRecordNotFound(f"file not found: {file_uid}")
        if current.modality_summary_kind is not None and current.modality_summary_kind != kind:
            raise ModalitySummaryConflict(
                f"file {file_uid} already has {current.modality_summary_kind!r} summary; "
                f"cannot attach {kind!r}"
            )
        try:
            normalized_summary = json.loads(json_dumps(dict(summary)))
        except (TypeError, ValueError) as exc:
            raise MetadataValidationError(f"modality summary is not JSON-serializable: {exc}") from exc

        tag_updates: dict[str, list[str]] = {}
        if kind == "image":
            visual_tags = normalized_summary.get("visual_tags")
            tag_updates["visual"] = _normalize_tags(
                visual_tags if isinstance(visual_tags, list) else []
            )
        elif kind == "table":
            column_names = normalized_summary.get("column_names")
            tag_updates["column"] = _normalize_tags(
                column_names if isinstance(column_names, list) else []
            )
        for tag_kind, values in (extra_tags or {}).items():
            # Layer 2 tags live in dedicated JSON columns; modality summaries
            # may only contribute their own inverted tag namespaces.
            if tag_kind not in {"visual", "column"}:
                raise MetadataValidationError(
                    f"modality extra_tags may only use visual/column, got {tag_kind!r}"
                )
            tag_updates[tag_kind] = _normalize_tags(values)

        now = iso_utc(utc_now())
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE files SET modality_summary_kind = ?, modality_summary = ?,
                                 updated_at = ?
                WHERE file_uid = ?
                """,
                (kind, json_dumps(normalized_summary), now, file_uid),
            )
            for tag_kind, values in tag_updates.items():
                self._replace_tags(conn, file_uid, tag_kind, values)

        record = self.get_file(file_uid)
        if record is None:  # pragma: no cover - defensive
            raise FileRecordNotFound(f"file not found: {file_uid}")
        return record

    def clear_modality_summary(self, file_uid: str) -> FileRecord:
        now = iso_utc(utc_now())
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE files SET modality_summary_kind = NULL, modality_summary = NULL,
                                 updated_at = ?
                WHERE file_uid = ?
                """,
                (now, file_uid),
            )
            if cursor.rowcount == 0:
                raise FileRecordNotFound(f"file not found: {file_uid}")
            conn.execute(
                """
                DELETE FROM file_tags
                WHERE file_uid = ? AND tag_id IN (
                    SELECT tag_id FROM tags WHERE kind IN ('visual', 'column')
                )
                """,
                (file_uid,),
            )
            self._delete_orphan_tags(conn)
        record = self.get_file(file_uid)
        if record is None:  # pragma: no cover - defensive
            raise FileRecordNotFound(f"file not found: {file_uid}")
        return record

    def _replace_tags(
        self, conn: sqlite3.Connection, file_uid: str, kind: str, values: Sequence[str]
    ) -> None:
        conn.execute(
            """
            DELETE FROM file_tags
            WHERE file_uid = ? AND tag_id IN (SELECT tag_id FROM tags WHERE kind = ?)
            """,
            (file_uid, kind),
        )
        for value in values:
            conn.execute("INSERT OR IGNORE INTO tags (kind, value) VALUES (?, ?)", (kind, value))
            row = conn.execute(
                "SELECT tag_id FROM tags WHERE kind = ? AND value = ?", (kind, value)
            ).fetchone()
            if row is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO file_tags (file_uid, tag_id) VALUES (?, ?)",
                    (file_uid, row["tag_id"]),
                )
        self._delete_orphan_tags(conn)

    @staticmethod
    def _delete_orphan_tags(conn: sqlite3.Connection) -> None:
        conn.execute(
            "DELETE FROM tags WHERE tag_id NOT IN (SELECT tag_id FROM file_tags)"
        )

    # ------------------------------------------------------------------- read
    def get_file(self, file_uid: str) -> FileRecord | None:
        row = self._db.fetchone(
            "SELECT * FROM files WHERE file_uid = ? AND deleted_at IS NULL", (file_uid,)
        )
        return None if row is None else FileRecord.from_row(row)

    def get_file_by_path(
        self, storage_mode: StorageMode | str, local_path: str | Path
    ) -> FileRecord | None:
        try:
            mode = StorageMode(storage_mode).value
        except ValueError as exc:
            raise MetadataValidationError(
                f"unsupported storage_mode: {storage_mode!r}"
            ) from exc
        row = self._db.fetchone(
            """
            SELECT * FROM files
            WHERE storage_mode = ? AND local_path = ? AND deleted_at IS NULL
            """,
            (mode, str(local_path)),
        )
        return None if row is None else FileRecord.from_row(row)

    def get_file_by_uri(self, uri: str) -> FileRecord | None:
        row = self._db.fetchone(
            "SELECT * FROM files WHERE uri = ? AND deleted_at IS NULL", (uri,)
        )
        return None if row is None else FileRecord.from_row(row)

    def find_by_hash(
        self, content_hash: str, *, storage_mode: StorageMode | str | None = None
    ) -> list[FileRecord]:
        if storage_mode is None:
            rows = self._db.fetchall(
                """
                SELECT * FROM files
                WHERE content_hash = ? AND deleted_at IS NULL
                ORDER BY registered_at, file_uid
                """,
                (content_hash,),
            )
        else:
            try:
                mode = StorageMode(storage_mode).value
            except ValueError as exc:
                raise MetadataValidationError(
                    f"unsupported storage_mode: {storage_mode!r}"
                ) from exc
            rows = self._db.fetchall(
                """
                SELECT * FROM files
                WHERE content_hash = ? AND storage_mode = ? AND deleted_at IS NULL
                ORDER BY registered_at, file_uid
                """,
                (content_hash, mode),
            )
        return [FileRecord.from_row(row) for row in rows]

    def get_files_by_uids(self, uids: Sequence[str]) -> list[FileRecord]:
        unique = list(dict.fromkeys(str(uid) for uid in uids))
        if not unique:
            return []
        results: list[FileRecord] = []
        for start in range(0, len(unique), 500):
            batch = unique[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = self._db.fetchall(
                f"SELECT * FROM files WHERE file_uid IN ({placeholders}) AND deleted_at IS NULL",
                batch,
            )
            results.extend(FileRecord.from_row(row) for row in rows)
        by_uid = {record.file_uid: record for record in results}
        return [by_uid[uid] for uid in unique if uid in by_uid]

    def query_files(self, query: FileQuery | None = None) -> list[FileRecord]:
        query = query or FileQuery()
        conditions, params = self.build_filter_sql(query)
        order_column = query.order_column()
        direction = "DESC" if query.descending else "ASC"
        sql = (
            f"SELECT * FROM files WHERE {' AND '.join(conditions)} "
            f"ORDER BY {order_column} {direction}, file_uid ASC"
        )
        if query.limit is not None:
            if int(query.limit) < 0:
                raise MetadataValidationError("limit must be non-negative")
            sql += " LIMIT ? OFFSET ?"
            params.extend([int(query.limit), max(0, int(query.offset))])
        rows = self._db.fetchall(sql, params)
        return [FileRecord.from_row(row) for row in rows]

    def count_files(self) -> int:
        row = self._db.fetchone("SELECT COUNT(*) AS n FROM files WHERE deleted_at IS NULL")
        return int(row["n"]) if row else 0

    def stats(self) -> dict[str, Any]:
        total = self._db.fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes "
            "FROM files WHERE deleted_at IS NULL"
        )
        by_extension = self._db.fetchall(
            """
            SELECT extension, COUNT(*) AS n FROM files
            WHERE deleted_at IS NULL GROUP BY extension ORDER BY n DESC, extension
            """
        )
        by_mode = self._db.fetchall(
            """
            SELECT storage_mode, COUNT(*) AS n FROM files
            WHERE deleted_at IS NULL GROUP BY storage_mode
            """
        )
        by_modality = self._db.fetchall(
            """
            SELECT modality_summary_kind, COUNT(*) AS n FROM files
            WHERE deleted_at IS NULL GROUP BY modality_summary_kind
            """
        )
        return {
            "total_files": int(total["n"]) if total else 0,
            "total_bytes": int(total["bytes"]) if total else 0,
            "by_extension": {row["extension"]: int(row["n"]) for row in by_extension},
            "by_storage_mode": {row["storage_mode"]: int(row["n"]) for row in by_mode},
            "by_modality": {
                (row["modality_summary_kind"] or "none"): int(row["n"]) for row in by_modality
            },
        }

    # --------------------------------------------------------------- filtering
    def build_filter_sql(
        self, query: FileQuery | None = None, *, alias: str = "files"
    ) -> tuple[list[str], list[Any]]:
        """Build reusable SQL predicates over the ``files`` table.

        Search code in later steps passes ``alias="fi"`` so the same filter
        semantics apply to BM25 and vector candidate queries.
        """
        if not _ALIAS_PATTERN.fullmatch(alias):
            raise MetadataValidationError(f"invalid SQL alias: {alias!r}")
        query = query or FileQuery()
        conditions: list[str] = [f"{alias}.deleted_at IS NULL"]
        params: list[Any] = []

        extensions = query.normalized_extensions()
        if extensions:
            placeholders = ",".join("?" for _ in extensions)
            conditions.append(f"{alias}.extension IN ({placeholders})")
            params.extend(extensions)

        if query.mime_types:
            mime_types = [str(item) for item in query.mime_types if str(item)]
            if mime_types:
                placeholders = ",".join("?" for _ in mime_types)
                conditions.append(f"{alias}.mime_type IN ({placeholders})")
                params.extend(mime_types)

        modes = query.normalized_modes()
        if modes:
            placeholders = ",".join("?" for _ in modes)
            conditions.append(f"{alias}.storage_mode IN ({placeholders})")
            params.extend(modes)

        if query.name_contains:
            conditions.append(f"instr(lower({alias}.file_name), lower(?)) > 0")
            params.append(query.name_contains)

        if query.parent_dir_prefix:
            conditions.append(
                f"substr({alias}.parent_dir, 1, length(?)) = ?"
            )
            params.extend([query.parent_dir_prefix, query.parent_dir_prefix])

        if query.min_size is not None:
            conditions.append(f"{alias}.size_bytes >= ?")
            params.append(int(query.min_size))
        if query.max_size is not None:
            conditions.append(f"{alias}.size_bytes <= ?")
            params.append(int(query.max_size))

        for value, operator in (
            (query.modified_after, ">="),
            (query.modified_before, "<="),
        ):
            if not value:
                continue
            try:
                normalized = iso_utc(
                    coerce_datetime(value, field="modified time filter")
                )
            except ValueError as exc:
                raise MetadataValidationError(str(exc)) from exc
            conditions.append(f"{alias}.fs_modified_at {operator} ?")
            params.append(normalized)

        if query.topic_tags:
            tags = _normalize_tag_input(query.topic_tags, "topic_tags")
            if tags:
                self._append_tag_filter(
                    conditions,
                    params,
                    alias,
                    "topic",
                    tags,
                    require_all=query.require_all_topic_tags,
                )
        if query.keywords:
            keywords = _normalize_tag_input(query.keywords, "keywords")
            if keywords:
                self._append_tag_filter(
                    conditions, params, alias, "keyword", keywords, require_all=False
                )
        if query.entities:
            entities = _normalize_tag_input(query.entities, "entities")
            if entities:
                self._append_tag_filter(
                    conditions, params, alias, "entity", entities, require_all=False
                )

        if query.business_start_from:
            try:
                start = coerce_iso_date(
                    query.business_start_from, field="business_start_from"
                )
            except ValueError as exc:
                raise MetadataValidationError(str(exc)) from exc
            if start:
                conditions.append(
                    f"{alias}.business_end_date IS NOT NULL AND "
                    f"{alias}.business_end_date >= ?"
                )
                params.append(start)
        if query.business_end_to:
            try:
                end = coerce_iso_date(query.business_end_to, field="business_end_to")
            except ValueError as exc:
                raise MetadataValidationError(str(exc)) from exc
            if end:
                conditions.append(
                    f"{alias}.business_start_date IS NOT NULL AND "
                    f"{alias}.business_start_date <= ?"
                )
                params.append(end)

        if query.modality_kind:
            if query.modality_kind not in MODALITY_KINDS:
                raise MetadataValidationError(
                    f"unsupported modality kind: {query.modality_kind!r}"
                )
            conditions.append(f"{alias}.modality_summary_kind = ?")
            params.append(query.modality_kind)

        for key, expected in (query.modality_filters or {}).items():
            if not _JSON_PATH_PATTERN.fullmatch(str(key)):
                raise MetadataValidationError(f"invalid modality filter key: {key!r}")
            json_path = f"$.{key}"
            if isinstance(expected, bool):
                conditions.append(f"json_extract({alias}.modality_summary, ?) = ?")
                params.extend([json_path, 1 if expected else 0])
            elif isinstance(expected, (list, tuple, set)):
                values = list(expected)
                if not values:
                    conditions.append("0")
                    continue
                placeholders = ",".join("?" for _ in values)
                conditions.append(
                    f"(json_extract({alias}.modality_summary, ?) IN ({placeholders}) "
                    f"OR EXISTS (SELECT 1 FROM json_each({alias}.modality_summary, ?) AS je "
                    f"WHERE je.value IN ({placeholders})))"
                )
                params.extend([json_path, *values, json_path, *values])
            elif expected is None:
                conditions.append(f"json_extract({alias}.modality_summary, ?) IS NULL")
                params.append(json_path)
            else:
                # Match either a scalar field or membership in an array field.
                conditions.append(
                    f"(json_extract({alias}.modality_summary, ?) = ? "
                    f"OR EXISTS (SELECT 1 FROM json_each({alias}.modality_summary, ?) AS je "
                    f"WHERE je.value = ?))"
                )
                params.extend([json_path, expected, json_path, expected])

        return conditions, params

    @staticmethod
    def _append_tag_filter(
        conditions: list[str],
        params: list[Any],
        alias: str,
        kind: str,
        values: Sequence[str],
        *,
        require_all: bool,
    ) -> None:
        placeholders = ",".join("?" for _ in values)
        if require_all:
            conditions.append(
                f"(SELECT COUNT(DISTINCT t.value) FROM file_tags ft "
                f"JOIN tags t ON t.tag_id = ft.tag_id "
                f"WHERE ft.file_uid = {alias}.file_uid AND t.kind = ? "
                f"AND t.value IN ({placeholders})) = ?"
            )
            params.extend([kind, *values, len(values)])
        else:
            conditions.append(
                f"EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.tag_id = ft.tag_id "
                f"WHERE ft.file_uid = {alias}.file_uid AND t.kind = ? "
                f"AND t.value IN ({placeholders}))"
            )
            params.extend([kind, *values])

    # ----------------------------------------------------------------- update
    def update_path(
        self,
        file_uid: str,
        new_local_path: str | Path,
        *,
        new_uri: str | None = None,
        relative_path: str | None | object = _UNSET,
        file_name: str | None = None,
        extension: str | None = None,
        mime_type: str | None = None,
    ) -> FileRecord:
        current = self.get_file(file_uid)
        if current is None:
            raise FileRecordNotFound(f"file not found: {file_uid}")
        path = Path(new_local_path)
        name = file_name or path.name
        ext = extension if extension is not None else file_extension(name)
        mime = mime_type or detect_mime_type(name)
        rel = current.relative_path if relative_path is _UNSET else relative_path
        uri = new_uri or f"local:{path}"
        now = iso_utc(utc_now())
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    """
                    UPDATE files SET file_name = ?, extension = ?, mime_type = ?,
                                     parent_dir = ?, local_path = ?, uri = ?,
                                     relative_path = ?, updated_at = ?
                    WHERE file_uid = ?
                    """,
                    (
                        name,
                        ext,
                        mime,
                        str(path.parent),
                        str(path),
                        uri,
                        rel,
                        now,
                        file_uid,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise MetadataError(f"path update conflicts with another record: {exc}") from exc
        record = self.get_file(file_uid)
        if record is None:  # pragma: no cover - defensive
            raise FileRecordNotFound(f"file not found: {file_uid}")
        return record

    def sync_path_by_hash(
        self,
        content_hash: str,
        new_local_path: str | Path,
        *,
        storage_mode: StorageMode | str | None = None,
        new_uri: str | None = None,
    ) -> FileRecord | None:
        """Update the path of a moved (content-identical) file.

        Returns ``None`` when no record shares the hash. Multiple matches are
        ambiguous and raise instead of guessing.
        """
        candidates = self.find_by_hash(content_hash, storage_mode=storage_mode)
        if not candidates:
            return None
        if len(candidates) > 1:
            raise AmbiguousContentHashError(
                f"content hash {content_hash} matches {len(candidates)} files; "
                "pass an explicit file UID instead"
            )
        return self.update_path(
            candidates[0].file_uid, new_local_path, new_uri=new_uri
        )

    # ----------------------------------------------------------------- delete
    def delete_file(self, file_uid: str) -> bool:
        """Hard-delete metadata. Index rows must be removed by the caller first."""
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM files WHERE file_uid = ?", (file_uid,))
        return cursor.rowcount > 0
