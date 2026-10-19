"""Schema for the Step 1 metadata center and hybrid index."""

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    file_uid            TEXT PRIMARY KEY,
    file_name           TEXT NOT NULL,
    extension           TEXT NOT NULL DEFAULT '',
    mime_type           TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL CHECK (size_bytes >= 0),
    parent_dir          TEXT NOT NULL,
    storage_mode        TEXT NOT NULL CHECK (storage_mode IN ('inplace', 'managed')),
    content_hash        TEXT NOT NULL,
    uri                 TEXT NOT NULL,
    local_path          TEXT NOT NULL,
    relative_path       TEXT,
    fs_created_at       TEXT NOT NULL,
    fs_modified_at      TEXT NOT NULL,
    fs_created_ts       REAL NOT NULL,
    fs_modified_ts      REAL NOT NULL,
    registered_at       TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    topic_tags          TEXT NOT NULL DEFAULT '[]',
    summary             TEXT,
    entities            TEXT NOT NULL DEFAULT '[]',
    keywords            TEXT NOT NULL DEFAULT '[]',
    business_start_date TEXT,
    business_end_date   TEXT,
    modality_summary_kind TEXT CHECK (
        modality_summary_kind IS NULL
        OR modality_summary_kind IN ('office', 'image', 'table')
    ),
    modality_summary    TEXT,
    deleted_at          TEXT,
    UNIQUE (storage_mode, local_path),
    UNIQUE (uri)
);

CREATE INDEX IF NOT EXISTS idx_files_content_hash ON files (content_hash);
CREATE INDEX IF NOT EXISTS idx_files_extension ON files (extension);
CREATE INDEX IF NOT EXISTS idx_files_modified ON files (fs_modified_ts);
CREATE INDEX IF NOT EXISTS idx_files_size ON files (size_bytes);
CREATE INDEX IF NOT EXISTS idx_files_mode ON files (storage_mode);
CREATE INDEX IF NOT EXISTS idx_files_modality_kind ON files (modality_summary_kind);
CREATE INDEX IF NOT EXISTS idx_files_business_range
    ON files (business_start_date, business_end_date);
-- Composite indexes for the common compound-filter shapes described in Step 1.
CREATE INDEX IF NOT EXISTS idx_files_ext_modified
    ON files (extension, fs_modified_ts);
CREATE INDEX IF NOT EXISTS idx_files_ext_size
    ON files (extension, size_bytes);
CREATE INDEX IF NOT EXISTS idx_files_mode_modified
    ON files (storage_mode, fs_modified_ts);
CREATE INDEX IF NOT EXISTS idx_files_ext_business
    ON files (extension, business_start_date, business_end_date);

CREATE TABLE IF NOT EXISTS tags (
    tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind   TEXT NOT NULL CHECK (kind IN ('topic', 'keyword', 'entity', 'visual', 'column')),
    value  TEXT NOT NULL,
    UNIQUE (kind, value)
);

CREATE TABLE IF NOT EXISTS file_tags (
    file_uid TEXT NOT NULL REFERENCES files (file_uid) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags (tag_id) ON DELETE CASCADE,
    PRIMARY KEY (file_uid, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags (tag_id, file_uid);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_uid       TEXT NOT NULL UNIQUE,
    file_uid        TEXT NOT NULL REFERENCES files (file_uid) ON DELETE CASCADE,
    chunk_order     INTEGER NOT NULL CHECK (chunk_order >= 0),
    chunk_type      TEXT NOT NULL DEFAULT 'text',
    page_start      INTEGER,
    page_end        INTEGER,
    sheet_name      TEXT,
    row_start       INTEGER,
    row_end         INTEGER,
    col_start       INTEGER,
    col_end         INTEGER,
    bbox            TEXT,
    image_path      TEXT,
    structured_text TEXT NOT NULL DEFAULT '',
    plain_text      TEXT NOT NULL DEFAULT '',
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (file_uid, chunk_order)
);

CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks (file_uid);
CREATE INDEX IF NOT EXISTS idx_chunks_file_page ON chunks (file_uid, page_start);
CREATE INDEX IF NOT EXISTS idx_chunks_sheet ON chunks (file_uid, sheet_name);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    chunk_uid UNINDEXED,
    file_uid UNINDEXED,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""
