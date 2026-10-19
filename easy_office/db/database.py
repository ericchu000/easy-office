"""Shared SQLite connection with WAL, FTS5 and sqlite-vec support."""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

try:  # sqlite-vec ships a native extension loaded into SQLite.
    import sqlite_vec
except ImportError as exc:  # pragma: no cover - environment failure
    raise RuntimeError(
        "sqlite-vec is required. Install it with: uv add sqlite-vec"
    ) from exc

from easy_office.db.schema import SCHEMA_SQL, SCHEMA_VERSION


class DatabaseError(Exception):
    """Base class for database failures."""


class VectorDimensionMismatch(DatabaseError):
    """Raised when vectors or the index table disagree with the configured dim."""


class SchemaVersionError(DatabaseError):
    """Raised when the database was created by a newer application version."""


_VECTOR_DIM_PATTERN = re.compile(r"float\s*\[\s*(\d+)\s*\]", re.IGNORECASE)


class Database:
    """One serialized SQLite connection shared by metadata and index stores.

    Writers must use :meth:`transaction`; reads may use :meth:`fetchone` and
    :meth:`fetchall`. ``check_same_thread=False`` plus an internal RLock keeps
    the connection safe for the daemon's thread pool while Step 5's multiprocess
    indexing still funnels writes through the main process.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        embedding_dim: int,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._lock = threading.RLock()
        self._path = Path(db_path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._dim = int(embedding_dim)
        if self._dim <= 0:
            raise ValueError("embedding_dim must be positive")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect(self._path)
        self._init_schema()
        self._ensure_vector_table(self._dim)

    # ------------------------------------------------------------- properties
    @property
    def path(self) -> Path:
        return self._path

    @property
    def embedding_dim(self) -> int:
        return self._dim

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DatabaseError("database connection is closed")
        return self._conn

    # ------------------------------------------------------------- connection
    def _connect(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            isolation_level=None,
            timeout=max(1.0, self._busy_timeout_ms / 1000.0),
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode = WAL").fetchall()
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA_SQL)
            row = self.conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None and int(row["value"]) > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database schema version {row['value']} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            self.conn.execute(
                """
                INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------- transactions
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self.conn
            if conn.in_transaction:
                raise DatabaseError("nested transactions are not supported")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.executemany(sql, rows)

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------- schema meta
    def _get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO schema_meta (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)
        ).fetchone()
        return row is not None

    # -------------------------------------------------------------- vector dim
    def _ensure_vector_table(self, dim: int) -> None:
        if dim <= 0:
            raise ValueError("embedding_dim must be positive")
        with self._lock:
            if self.table_exists("chunk_vec"):
                table_sql = self.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'chunk_vec'"
                ).fetchone()
                match = _VECTOR_DIM_PATTERN.search(table_sql["sql"] if table_sql else "")
                if match is None:
                    raise DatabaseError("cannot determine chunk_vec dimension")
                actual = int(match.group(1))
                stored = self._get_meta("embedding_dim")
                if stored is None:
                    self._set_meta("embedding_dim", str(actual))
                elif int(stored) != actual:
                    raise VectorDimensionMismatch(
                        f"vector table dimension {actual} disagrees with metadata {stored}"
                    )
                if actual != dim:
                    raise VectorDimensionMismatch(
                        f"index was built with embedding_dim={actual}, "
                        f"but configuration specifies {dim}; rebuild the vector index"
                    )
            else:
                self.conn.execute(
                    f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{dim}])"
                )
                self._set_meta("embedding_dim", str(dim))
            self._dim = dim

    def vector_count(self) -> int:
        with self._lock:
            if not self.table_exists("chunk_vec"):
                return 0
            return int(self.conn.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0])

    def assert_dimension_change_allowed(self, dim: int) -> None:
        """Validate a configuration change without mutating the database."""
        if int(dim) <= 0:
            raise VectorDimensionMismatch("embedding_dim must be positive")
        if int(dim) == self._dim:
            return
        count = self.vector_count()
        if count > 0:
            raise VectorDimensionMismatch(
                f"cannot change embedding_dim from {self._dim} to {dim}: "
                f"{count} vectors already exist; clear or rebuild the index first"
            )

    def set_embedding_dim(self, dim: int, *, allow_rebuild: bool = False) -> None:
        """Change the vector dimension, rebuilding the table when permitted."""
        dim = int(dim)
        if dim <= 0:
            raise VectorDimensionMismatch("embedding_dim must be positive")
        with self._lock:
            if dim == self._dim:
                return
            count = self.vector_count()
            if count > 0 and not allow_rebuild:
                raise VectorDimensionMismatch(
                    f"cannot change embedding_dim from {self._dim} to {dim}: "
                    f"{count} vectors already exist"
                )
            self.conn.execute("DROP TABLE IF EXISTS chunk_vec")
            self.conn.execute(
                f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{dim}])"
            )
            self._set_meta("embedding_dim", str(dim))
            self._dim = dim

    def reopen(self, db_path: str | Path, *, embedding_dim: int | None = None) -> None:
        """Switch to another database file while keeping callers' references valid."""
        target = Path(db_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            old_path, old_conn, old_dim = self._path, self._conn, self._dim
            new_conn: sqlite3.Connection | None = None
            try:
                new_conn = self._connect(target)
                self._conn = new_conn
                self._path = target
                self._dim = int(embedding_dim if embedding_dim is not None else old_dim)
                self._init_schema()
                self._ensure_vector_table(self._dim)
            except BaseException:
                if new_conn is not None:
                    try:
                        new_conn.close()
                    except sqlite3.Error:
                        pass
                self._path, self._conn, self._dim = old_path, old_conn, old_dim
                raise
            if old_conn is not None:
                old_conn.close()
