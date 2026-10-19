"""SQLite FTS5 + sqlite-vec implementation of the hybrid chunk index."""

from __future__ import annotations

import logging
import math
import sqlite3
from typing import Any, Sequence

import sqlite_vec

from easy_office.db.database import Database, VectorDimensionMismatch
from easy_office.indexing.base import (
    ChunkInput,
    ChunkRecord,
    HybridIndexStorage,
    SearchHit,
)
from easy_office.indexing.mirror import MirrorCache
from easy_office.indexing.tokenizer import (
    build_match_expression,
    make_snippet,
    normalize_for_index,
    plain_text_for_index,
    tokenize,
)
from easy_office.metadata.models import FileQuery, FileRecord
from easy_office.metadata.repository import MetadataStore
from easy_office.utils import iso_utc, json_dumps, new_uid, utc_now

logger = logging.getLogger(__name__)

_MAX_VECTOR_FILTER_IDS = 20_000


class SQLiteHybridIndexStore(HybridIndexStorage):
    """Default lightweight index: FTS5 for BM25, sqlite-vec for dense vectors."""

    def __init__(
        self,
        db: Database,
        metadata: MetadataStore,
        mirror: MirrorCache | None = None,
    ) -> None:
        self._db = db
        self._metadata = metadata
        self._mirror = mirror

    @property
    def embedding_dim(self) -> int:
        return self._db.embedding_dim

    # ------------------------------------------------------------------ write
    def upsert_chunks(self, file_uid: str, chunks: Sequence[ChunkInput]) -> list[ChunkRecord]:
        items = list(chunks)
        if not items:
            return []
        if self._metadata.get_file(file_uid) is None:
            raise ValueError(f"cannot index chunks for unknown file: {file_uid}")

        seen_orders: set[int] = set()
        for chunk in items:
            if chunk.chunk_order in seen_orders:
                raise ValueError(f"duplicate chunk_order {chunk.chunk_order} for {file_uid}")
            seen_orders.add(chunk.chunk_order)
            if chunk.embedding is not None:
                self._check_vector(chunk.embedding, context="chunk embedding")

        now = iso_utc(utc_now())
        written_ids: list[int] = []
        with self._db.transaction() as conn:
            for chunk in items:
                existing = conn.execute(
                    "SELECT chunk_id, chunk_uid, created_at FROM chunks "
                    "WHERE file_uid = ? AND chunk_order = ?",
                    (file_uid, chunk.chunk_order),
                ).fetchone()
                structured = chunk.structured_text or chunk.plain_text
                # plain_text is the markup-free column consumed by FTS, the
                # mirror cache and snippets; structured_text keeps the original.
                plain = plain_text_for_index(chunk.plain_text or structured)
                bbox = json_dumps(list(chunk.bbox)) if chunk.bbox is not None else None
                metadata_json = json_dumps(dict(chunk.metadata or {}))

                if existing is not None:
                    chunk_id = int(existing["chunk_id"])
                    chunk_uid = existing["chunk_uid"]
                    created_at = existing["created_at"]
                    conn.execute(
                        """
                        UPDATE chunks SET
                            chunk_type = ?, page_start = ?, page_end = ?, sheet_name = ?,
                            row_start = ?, row_end = ?, col_start = ?, col_end = ?,
                            bbox = ?, image_path = ?, structured_text = ?,
                            plain_text = ?, metadata = ?, updated_at = ?
                        WHERE chunk_id = ?
                        """,
                        (
                            chunk.chunk_type,
                            chunk.page_start,
                            chunk.page_end,
                            chunk.sheet_name,
                            chunk.row_start,
                            chunk.row_end,
                            chunk.col_start,
                            chunk.col_end,
                            bbox,
                            chunk.image_path,
                            structured,
                            plain,
                            metadata_json,
                            now,
                            chunk_id,
                        ),
                    )
                    conn.execute("DELETE FROM chunk_fts WHERE rowid = ?", (chunk_id,))
                    conn.execute("DELETE FROM chunk_vec WHERE rowid = ?", (chunk_id,))
                else:
                    chunk_uid = chunk.chunk_uid or new_uid()
                    try:
                        cursor = conn.execute(
                            """
                            INSERT INTO chunks (
                                chunk_uid, file_uid, chunk_order, chunk_type,
                                page_start, page_end, sheet_name,
                                row_start, row_end, col_start, col_end,
                                bbox, image_path, structured_text, plain_text,
                                metadata, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                chunk_uid,
                                file_uid,
                                chunk.chunk_order,
                                chunk.chunk_type,
                                chunk.page_start,
                                chunk.page_end,
                                chunk.sheet_name,
                                chunk.row_start,
                                chunk.row_end,
                                chunk.col_start,
                                chunk.col_end,
                                bbox,
                                chunk.image_path,
                                structured,
                                plain,
                                metadata_json,
                                now,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ValueError(f"cannot insert chunk for {file_uid}: {exc}") from exc
                    chunk_id = int(cursor.lastrowid)
                    created_at = now
                written_ids.append(chunk_id)

                body = normalize_for_index(plain)
                conn.execute(
                    "INSERT INTO chunk_fts (rowid, chunk_uid, file_uid, body) VALUES (?, ?, ?, ?)",
                    (chunk_id, chunk_uid, file_uid, body),
                )
                if chunk.embedding is not None:
                    conn.execute(
                        "INSERT INTO chunk_vec (rowid, embedding) VALUES (?, ?)",
                        (chunk_id, sqlite_vec.serialize_float32(self._normalize(chunk.embedding))),
                    )

        if self._mirror is not None:
            self._mirror.sync_chunks(file_uid, self.get_chunks(file_uid))
        if not written_ids:
            return []
        placeholders = ",".join("?" for _ in written_ids)
        rows = self._db.fetchall(
            f"SELECT * FROM chunks WHERE chunk_id IN ({placeholders}) ORDER BY chunk_order ASC",
            written_ids,
        )
        return [ChunkRecord.from_row(row) for row in rows]

    def replace_file_chunks(
        self, file_uid: str, chunks: Sequence[ChunkInput]
    ) -> list[ChunkRecord]:
        self.delete_chunks_for_file(file_uid)
        return self.upsert_chunks(file_uid, chunks)

    def delete_chunks_for_file(self, file_uid: str) -> int:
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT chunk_id FROM chunks WHERE file_uid = ?", (file_uid,)
            ).fetchall()
            chunk_ids = [int(row["chunk_id"]) for row in rows]
            if chunk_ids:
                conn.executemany(
                    "DELETE FROM chunk_fts WHERE rowid = ?", [(cid,) for cid in chunk_ids]
                )
                conn.executemany(
                    "DELETE FROM chunk_vec WHERE rowid = ?", [(cid,) for cid in chunk_ids]
                )
                conn.executemany(
                    "DELETE FROM chunks WHERE chunk_id = ?", [(cid,) for cid in chunk_ids]
                )
        if self._mirror is not None:
            self._mirror.delete(file_uid)
        return len(chunk_ids)

    # ------------------------------------------------------------------- read
    def count_chunks(self, file_uid: str | None = None) -> int:
        if file_uid is None:
            row = self._db.fetchone("SELECT COUNT(*) AS n FROM chunks")
        else:
            row = self._db.fetchone(
                "SELECT COUNT(*) AS n FROM chunks WHERE file_uid = ?", (file_uid,)
            )
        return int(row["n"]) if row else 0

    def get_chunks(
        self,
        file_uid: str,
        *,
        page_start: int | None = None,
        page_end: int | None = None,
        limit: int | None = None,
    ) -> list[ChunkRecord]:
        conditions = ["c.file_uid = ?"]
        params: list[Any] = [file_uid]
        if page_start is not None:
            conditions.append("(c.page_end IS NULL OR c.page_end >= ?)")
            params.append(int(page_start))
        if page_end is not None:
            conditions.append("(c.page_start IS NULL OR c.page_start <= ?)")
            params.append(int(page_end))
        sql = f"SELECT c.* FROM chunks c WHERE {' AND '.join(conditions)} ORDER BY c.chunk_order ASC"
        if limit is not None:
            if int(limit) < 0:
                raise ValueError("limit must be non-negative")
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._db.fetchall(sql, params)
        return [ChunkRecord.from_row(row) for row in rows]

    def get_chunk(self, chunk_id: int) -> ChunkRecord | None:
        row = self._db.fetchone("SELECT * FROM chunks WHERE chunk_id = ?", (int(chunk_id),))
        return None if row is None else ChunkRecord.from_row(row)

    # ---------------------------------------------------------------- retrieval
    def search_bm25(
        self,
        query: str,
        *,
        file_filter: FileQuery | None = None,
        top_k: int = 10,
        operator: str = "OR",
    ) -> list[SearchHit]:
        top_k = self._check_top_k(top_k)
        expression = build_match_expression(query, operator=operator)
        if expression is None:
            return []
        conditions, params = self._metadata.build_filter_sql(file_filter, alias="fi")
        sql = (
            "SELECT chunk_fts.rowid AS chunk_id, bm25(chunk_fts) AS raw_score "
            "FROM chunk_fts "
            "JOIN chunks c ON c.chunk_id = chunk_fts.rowid "
            "JOIN files fi ON fi.file_uid = c.file_uid "
            "WHERE chunk_fts MATCH ? AND "
            + " AND ".join(conditions)
            + " ORDER BY raw_score ASC, c.chunk_id ASC LIMIT ?"
        )
        rows = self._db.fetchall(sql, [expression, *params, top_k])
        ids = [int(row["chunk_id"]) for row in rows]
        scores = {int(row["chunk_id"]): -float(row["raw_score"]) for row in rows}
        return self._build_hits(
            ids,
            scores,
            source="bm25",
            snippet_tokens=tokenize(query),
        )

    def search_vector(
        self,
        query_vector: Sequence[float],
        *,
        file_filter: FileQuery | None = None,
        top_k: int = 10,
    ) -> list[SearchHit]:
        top_k = self._check_top_k(top_k)
        values = self._check_vector(query_vector, context="query vector")
        normalized = self._normalize(values)
        if not any(normalized):
            raise ValueError("query vector must be non-zero")
        serialized = sqlite_vec.serialize_float32(normalized)

        if file_filter is not None:
            candidate_ids = self._candidate_chunk_ids(file_filter)
            if not candidate_ids:
                return []
            placeholders = ",".join("?" for _ in candidate_ids)
            sql = (
                "SELECT rowid AS chunk_id, distance FROM chunk_vec "
                f"WHERE embedding MATCH ? AND rowid IN ({placeholders}) "
                "ORDER BY distance LIMIT ?"
            )
            rows = self._db.fetchall(sql, [serialized, *candidate_ids, top_k])
        else:
            rows = self._db.fetchall(
                "SELECT rowid AS chunk_id, distance FROM chunk_vec "
                "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (serialized, top_k),
            )

        ids = [int(row["chunk_id"]) for row in rows]
        scores: dict[int, float] = {}
        for row in rows:
            distance = float(row["distance"])
            # Stored vectors are L2-normalized, so ||a-b||^2 = 2 - 2*cos.
            scores[int(row["chunk_id"])] = max(-1.0, min(1.0, 1.0 - (distance * distance) / 2.0))
        return self._build_hits(ids, scores, source="vector", snippet_tokens=[])

    # --------------------------------------------------------------- internals
    def _candidate_chunk_ids(self, file_filter: FileQuery) -> list[int]:
        conditions, params = self._metadata.build_filter_sql(file_filter, alias="fi")
        sql = (
            "SELECT c.chunk_id FROM chunks c "
            "JOIN files fi ON fi.file_uid = c.file_uid "
            "WHERE " + " AND ".join(conditions) + " ORDER BY c.chunk_id LIMIT ?"
        )
        rows = self._db.fetchall(sql, [*params, _MAX_VECTOR_FILTER_IDS + 1])
        ids = [int(row["chunk_id"]) for row in rows]
        if len(ids) > _MAX_VECTOR_FILTER_IDS:
            ids = ids[:_MAX_VECTOR_FILTER_IDS]
            logger.warning(
                "vector filter matched more than %d chunks; candidate set was truncated",
                _MAX_VECTOR_FILTER_IDS,
            )
        return ids

    def _build_hits(
        self,
        ordered_ids: Sequence[int],
        scores: dict[int, float],
        *,
        source: str,
        snippet_tokens: Sequence[str],
    ) -> list[SearchHit]:
        if not ordered_ids:
            return []
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self._db.fetchall(
            f"SELECT * FROM chunks WHERE chunk_id IN ({placeholders})", list(ordered_ids)
        )
        by_id = {int(row["chunk_id"]): ChunkRecord.from_row(row) for row in rows}
        file_uids = [by_id[cid].file_uid for cid in ordered_ids if cid in by_id]
        files = {record.file_uid: record for record in self._metadata.get_files_by_uids(file_uids)}
        hits: list[SearchHit] = []
        rank = 0
        for chunk_id in ordered_ids:
            chunk = by_id.get(chunk_id)
            if chunk is None:
                continue
            record: FileRecord | None = files.get(chunk.file_uid)
            if record is None:
                continue
            rank += 1
            snippet_source = plain_text_for_index(chunk.plain_text or chunk.structured_text)
            hits.append(
                SearchHit(
                    chunk=chunk,
                    file=record,
                    score=scores.get(chunk_id, 0.0),
                    rank=rank,
                    snippet=make_snippet(snippet_source, list(snippet_tokens)),
                    source=source,
                )
            )
        return hits

    def _check_top_k(self, top_k: int) -> int:
        value = int(top_k)
        if value <= 0:
            raise ValueError("top_k must be positive")
        return value

    def _check_vector(self, vector: Sequence[float], *, context: str) -> list[float]:
        values = [float(item) for item in vector]
        if len(values) != self._db.embedding_dim:
            raise VectorDimensionMismatch(
                f"{context} has dimension {len(values)}, expected {self._db.embedding_dim}"
            )
        if any(not math.isfinite(item) for item in values):
            raise ValueError(f"{context} contains a non-finite value")
        return values

    @staticmethod
    def _normalize(vector: Sequence[float]) -> list[float]:
        norm = math.sqrt(sum(item * item for item in vector))
        if norm == 0.0:
            return [0.0 for _ in vector]
        return [item / norm for item in vector]
