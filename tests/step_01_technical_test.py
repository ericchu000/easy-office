"""Step 1 developer-facing tests.

Run with::

    .venv\\Scripts\\python.exe -m pytest tests/step_01_technical_test.py -v
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
from pathlib import Path

import pytest

from easy_office.config.manager import ConfigManager, ConfigValidationError
from easy_office.core import ContentChangedError, EasyOfficeCore
from easy_office.db.database import VectorDimensionMismatch
from easy_office.indexing.base import ChunkInput
from easy_office.indexing.mirror import MirrorLine
from easy_office.metadata.models import (
    AmbiguousContentHashError,
    FileQuery,
    MetadataValidationError,
    ModalitySummaryConflict,
)
from easy_office.storage.base import PathNotAllowedError, StorageError

DIM = 1024
REAL_SERVICES = os.environ.get("EASYOFFICE_REAL_SERVICES") == "1"
REAL_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


def _write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unit_vector(index: int, dim: int = DIM) -> list[float]:
    values = [0.0] * dim
    values[index] = 1.0
    return values


def _blend(first: int, second: int, second_weight: float = 0.05, dim: int = DIM) -> list[float]:
    values = [0.0] * dim
    values[first] = 1.0 - second_weight
    values[second] = second_weight
    return values


# ---------------------------------------------------------------------------
# Configuration center
# ---------------------------------------------------------------------------


def test_config_defaults_created_and_dynamic_update(core, tmp_path):
    paths = core.config.paths()
    assert paths.config_path.is_file()
    assert paths.index_db_path.is_file()
    assert paths.managed_workspace.is_dir()
    assert paths.mirror_dir.is_dir()

    observed = []
    unsubscribe = core.config.subscribe(observed.append)
    watch_dir = tmp_path / "watch-me"
    updated = core.config.update(
        {
            "llm": {"temperature": 0.55, "model_name": "test-llm"},
            "vlm": {"max_image_resolution": 1024},
            "embedding": {"batch_size": 8},
            "reranker": {
                "enabled": True,
                "provider": "openai_compatible",
                "model_name": "test-reranker",
                "top_n": 5,
            },
            "local_parser": {
                "device": "cuda",
                "weights_dir": "custom-weights",
                "ocr_languages": ["ch_sim", "en"],
            },
            "storage": {"host_watch_dirs": [str(watch_dir)]},
        }
    )
    unsubscribe()

    assert updated.llm.temperature == 0.55
    assert updated.reranker.enabled and updated.reranker.model_name == "test-reranker"
    assert updated.local_parser.device == "cuda"
    assert len(observed) == 1 and observed[0].llm.temperature == 0.55

    persisted = ConfigManager(config_path=paths.config_path, data_dir=paths.data_dir)
    assert persisted.get().llm.model_name == "test-llm"
    assert persisted.get().vlm.max_image_resolution == 1024
    assert persisted.get().embedding.batch_size == 8
    assert persisted.get().reranker.top_n == 5
    assert persisted.get().local_parser.weights_dir == "custom-weights"
    assert persisted.paths().host_watch_dirs == (watch_dir.resolve(),)
    assert persisted.paths().weights_dir == (paths.config_path.parent / "custom-weights")


@pytest.mark.parametrize(
    "bad_patch",
    [
        {"llm": {"temperature": 2.5}},
        {"embedding": {"embedding_dim": 0}},
        {"embedding": {"batch_size": 0}},
        {"local_parser": {"device": "tpu"}},
        {"concurrency": {"worker_processes": 0}},
        {"llm": {"base_url": "ftp://example"}},
        {"llm": {"unknown_field": 1}},
        {"reranker": {"enabled": True, "provider": "disabled"}},
        {"gateway": {"platforms": {"slack": {}}}},
        {"gateway": {"port": 70000}},
    ],
)
def test_invalid_config_is_rejected_without_side_effects(core, bad_patch):
    before_raw = core.config.raw_config()
    before_file = core.config.config_path.read_bytes()
    with pytest.raises(ConfigValidationError):
        core.config.update(bad_patch)
    assert core.config.raw_config() == before_raw
    assert core.config.config_path.read_bytes() == before_file


def test_secret_masking_and_masked_roundtrip(core):
    core.config.update(
        {
            "llm": {"api_key": "sk-top-secret"},
            "vlm": {"api_key": "vlm-secret"},
            "gateway": {"platforms": {"telegram": {"token": "tg-secret"}}},
        }
    )
    public = core.config.public_config()
    dumped = json.dumps(public, ensure_ascii=False)
    assert "sk-top-secret" not in dumped
    assert "vlm-secret" not in dumped
    assert "tg-secret" not in dumped
    assert public["llm"]["api_key"] == "********"
    assert public["gateway"]["platforms"]["telegram"]["token"] == "********"

    raw = core.config.raw_config()
    assert raw["llm"]["api_key"] == "sk-top-secret"
    assert "sk-top-secret" in core.config.config_path.read_text(encoding="utf-8")

    # Saving the masked public view back must not erase secrets.
    core.config.update(public)
    assert core.config.get().llm.api_key.get_secret_value() == "sk-top-secret"
    assert (
        core.config.get().gateway.platforms["telegram"].token.get_secret_value()
        == "tg-secret"
    )


def test_embedding_dimension_guard_and_rebuild(core, tmp_path):
    path = _write(tmp_path / "guard.txt", b"guard")
    record = core.register_inplace(path)
    core.add_chunks(record.file_uid, [ChunkInput(0, "guard", embedding=_unit_vector(0))])
    assert core.db.vector_count() == 1

    with pytest.raises(VectorDimensionMismatch):
        core.config.update({"embedding": {"embedding_dim": 16}})
    assert core.config.get().embedding.embedding_dim == DIM
    assert core.db.embedding_dim == DIM

    core.index.delete_chunks_for_file(record.file_uid)
    core.config.update({"embedding": {"embedding_dim": 16}})
    assert core.db.embedding_dim == 16
    assert core.config.get().embedding.embedding_dim == 16

    core.add_chunks(record.file_uid, [ChunkInput(0, "sixteen", embedding=[1.0] * 16)])
    with pytest.raises(VectorDimensionMismatch):
        core.add_chunks(record.file_uid, [ChunkInput(1, "eight", embedding=[1.0] * 8)])


# ---------------------------------------------------------------------------
# Storage modes and layer 1 metadata
# ---------------------------------------------------------------------------


def test_dual_storage_modes_and_first_layer_attributes(core, tmp_path):
    content = "第一层元数据测试".encode("utf-8")
    source = _write(tmp_path / "host" / "季度报告.txt", content)
    stat = source.stat()
    record = core.register_inplace(source)

    assert record.storage_mode == "inplace"
    assert record.file_name == "季度报告.txt"
    assert record.extension == "txt"
    assert record.mime_type == "text/plain"
    assert record.size_bytes == len(content)
    assert record.content_hash == _sha(content)
    assert record.local_path == str(source.resolve())
    assert record.parent_dir == str(source.parent)
    assert record.fs_modified_ts == pytest.approx(stat.st_mtime, abs=0.01)
    assert record.fs_created_ts > 0
    assert record.uri.startswith("local:")

    upload_bytes = b"PK\x03\x04stream-docx-bytes"
    uploaded = core.ingest_upload(
        "客户名单.docx",
        io.BytesIO(upload_bytes),
        fs_created_at="2024-01-02T03:04:05Z",
        fs_modified_at="2024-02-03",
        relative_path="客户资料/客户名单.docx",
    )
    assert uploaded.storage_mode == "managed"
    assert uploaded.content_hash == _sha(upload_bytes)
    assert uploaded.size_bytes == len(upload_bytes)
    assert uploaded.mime_type.endswith("wordprocessingml.document")
    assert uploaded.fs_created_at == "2024-01-02T03:04:05+00:00"
    assert uploaded.fs_modified_at == "2024-02-03T00:00:00+00:00"
    assert uploaded.relative_path == "客户资料/客户名单.docx"
    managed_path = Path(uploaded.local_path)
    assert managed_path.is_file()
    assert managed_path.read_bytes() == upload_bytes
    assert str(core.storage.managed_workspace) in str(managed_path)

    # Re-registering the same path keeps the UID and refreshes the attributes.
    _write(source, b"changed-content")
    refreshed = core.register_inplace(source)
    assert refreshed.file_uid == record.file_uid
    assert refreshed.content_hash == _sha(b"changed-content")
    assert refreshed.size_bytes == len(b"changed-content")


def test_stream_read_copy_locate_and_managed_move(core, tmp_path):
    record = core.ingest_upload("stream.txt", io.BytesIO(b"stream-content"))
    with core.storage.open_stream(record.uri) as handle:
        assert handle.read() == b"stream-content"
    assert core.storage.locate(record.uri).is_file()

    copy_uri = core.storage.make_uri(
        core.storage.managed_workspace / "copies" / "stream-copy.txt"
    )
    copied = core.storage.copy(record.uri, copy_uri)
    assert core.storage.exists(copied)
    assert core.storage.compute_sha256(copied) == record.content_hash

    moved_uri = core.storage.make_uri(
        core.storage.managed_workspace / "moved" / "stream.txt"
    )
    moved = core.storage.move(record.uri, moved_uri)
    assert core.storage.exists(moved)
    assert not core.storage.exists(record.uri)

    assert core.storage.delete(copied) is True
    assert core.storage.delete(moved) is True

    host_file = _write(tmp_path / "never-delete.txt", b"host")
    with pytest.raises(PathNotAllowedError):
        core.storage.delete(core.storage.make_uri(host_file))
    assert host_file.is_file()


def test_allowed_roots_and_upload_limit(core_factory, tmp_path):
    host = tmp_path / "allowed"
    host.mkdir()
    outside = _write(tmp_path / "outside.txt", b"outside")
    inside = _write(host / "inside.txt", b"inside")

    core = core_factory(storage={"host_allowed_roots": [str(host)]})
    assert core.register_inplace(inside).storage_mode == "inplace"
    with pytest.raises(PathNotAllowedError):
        core.register_inplace(outside)

    # The allow-list is hot-reloadable.
    core.config.update({"storage": {"host_allowed_roots": [str(tmp_path)]}})
    assert core.register_inplace(outside).file_uid

    limited = core_factory(storage={"max_upload_bytes": 16})
    with pytest.raises(StorageError):
        limited.ingest_upload("big.bin", io.BytesIO(b"x" * 32))
    workspace = limited.storage.managed_workspace
    assert not [item for item in workspace.rglob("*") if item.is_file()]

    with pytest.raises(StorageError):
        limited.ingest_upload("text.txt", io.StringIO("not-bytes"))


def test_path_sync_by_hash_and_core_move(core_factory, tmp_path):
    host = tmp_path / "host"
    core = core_factory(storage={"host_allowed_roots": [str(host)]})
    content = b"movable-content"
    original = _write(host / "原文件.txt", content)
    record = core.register_inplace(original)

    moved = host / "归档" / "原文件.txt"
    moved.parent.mkdir(parents=True)
    shutil.move(str(original), str(moved))
    synced = core.sync_moved_path(record.file_uid, moved)
    assert synced.file_uid == record.file_uid
    assert synced.local_path == str(moved.resolve())
    assert synced.relative_path == str(Path("归档") / "原文件.txt")

    renamed = host / "归档二" / "改名后.txt"
    renamed.parent.mkdir(parents=True)
    moved_record = core.move_file(record.file_uid, renamed)
    assert moved_record.local_path == str(renamed.resolve())
    assert moved_record.file_name == "改名后.txt"
    assert moved_record.relative_path == str(Path("归档二") / "改名后.txt")
    assert renamed.is_file() and not moved.exists()

    _write(renamed, b"content-changed")
    with pytest.raises(ContentChangedError):
        core.sync_moved_path(record.file_uid, renamed)

    # Ambiguous identical content cannot be resolved by hash alone.
    _write(host / "a.txt", b"same")
    _write(host / "b.txt", b"same")
    core.register_inplace(host / "a.txt")
    core.register_inplace(host / "b.txt")
    with pytest.raises(AmbiguousContentHashError):
        core.metadata.sync_path_by_hash(_sha(b"same"), host / "c.txt")


# ---------------------------------------------------------------------------
# Layered metadata and compound filtering
# ---------------------------------------------------------------------------


def _seed_metadata_library(core: EasyOfficeCore, root: Path):
    records = {}
    sizes = {
        "财报.docx": 10,
        "技术规范.pdf": 20,
        "会议照片.png": 30,
        "销售.csv": 40,
        "合同.docx": 50,
        "其他.txt": 60,
    }
    for name, size in sizes.items():
        records[name] = core.register_inplace(_write(root / name, b"x" * size))

    core.metadata.update_semantic_metadata(
        records["财报.docx"].file_uid,
        topic_tags=["财务", "年度报告"],
        summary="年度财务经营分析。",
        keywords=["营收", "利润"],
        entities=["甲公司"],
        business_start_date="2024-01-01",
        business_end_date="2024-12-31",
    )
    core.metadata.update_modality_summary(
        records["财报.docx"].file_uid,
        "office",
        {
            "document_carrier_type": "digital",
            "page_count": 3,
            "has_tables": True,
            "has_formulas": False,
        },
    )

    core.metadata.update_semantic_metadata(
        records["技术规范.pdf"].file_uid,
        topic_tags=["技术规范"],
        summary="接口技术规范。",
        keywords=["接口"],
        entities=["研发部"],
        business_start_date="2024-02-01",
        business_end_date="2024-03-01",
    )
    core.metadata.update_modality_summary(
        records["技术规范.pdf"].file_uid,
        "office",
        {"document_carrier_type": "digital", "page_count": 10, "has_tables": False},
    )

    core.metadata.update_semantic_metadata(
        records["会议照片.png"].file_uid,
        topic_tags=["财务", "会议"],
        summary="财务会议现场照片。",
    )
    core.metadata.update_modality_summary(
        records["会议照片.png"].file_uid,
        "image",
        {
            "width": 1920,
            "height": 1080,
            "background_color": "白色",
            "scene_category": "会议白板",
            "visual_tags": ["人物", "会议室"],
        },
    )

    core.metadata.update_semantic_metadata(
        records["销售.csv"].file_uid,
        topic_tags=["财务", "业务数据表"],
        summary="区域销售数据。",
    )
    core.metadata.update_modality_summary(
        records["销售.csv"].file_uid,
        "table",
        {"column_names": ["销售额", "地区"], "row_count": 1000},
    )

    core.metadata.update_semantic_metadata(
        records["合同.docx"].file_uid,
        topic_tags=["财务", "合同"],
        summary="采购合同。",
        keywords=["甲方"],
        entities=["甲公司", "乙公司"],
        business_start_date="2024-05-01",
        business_end_date="2024-06-30",
    )
    core.metadata.update_modality_summary(
        records["合同.docx"].file_uid,
        "office",
        {"document_carrier_type": "scan", "page_count": 5, "has_tables": True},
    )

    core.metadata.update_semantic_metadata(
        records["其他.txt"].file_uid,
        topic_tags=["其他"],
        summary="其他资料。",
    )
    return records


def _names(records):
    return {record.file_name for record in records}


def test_layered_metadata_and_compound_filters(core, tmp_path):
    library = _seed_metadata_library(core, tmp_path / "library")
    assert core.metadata.count_files() == 6

    assert _names(core.metadata.query_files(FileQuery(topic_tags=["财务"]))) == {
        "财报.docx",
        "会议照片.png",
        "销售.csv",
        "合同.docx",
    }
    assert _names(
        core.metadata.query_files(
            FileQuery(topic_tags=["财务", "年度报告"], require_all_topic_tags=True)
        )
    ) == {"财报.docx"}
    assert _names(
        core.metadata.query_files(
            FileQuery(topic_tags=["财务"], extensions=["docx"])
        )
    ) == {"财报.docx", "合同.docx"}
    assert _names(
        core.metadata.query_files(
            FileQuery(
                business_start_from="2024-05-15",
                business_end_to="2024-06-15",
            )
        )
    ) == {"财报.docx", "合同.docx"}
    assert _names(
        core.metadata.query_files(
            FileQuery(
                modality_kind="image",
                modality_filters={"background_color": "白色"},
            )
        )
    ) == {"会议照片.png"}
    assert _names(
        core.metadata.query_files(
            FileQuery(modality_filters={"column_names": "销售额"})
        )
    ) == {"销售.csv"}
    assert _names(
        core.metadata.query_files(FileQuery(modality_filters={"has_tables": True}))
    ) == {"财报.docx", "合同.docx"}
    assert _names(core.metadata.query_files(FileQuery(keywords=["甲方"]))) == {"合同.docx"}
    assert _names(core.metadata.query_files(FileQuery(entities=["甲公司"]))) == {
        "财报.docx",
        "合同.docx",
    }
    assert _names(core.metadata.query_files(FileQuery(name_contains="合同"))) == {
        "合同.docx"
    }
    assert _names(core.metadata.query_files(FileQuery(min_size=45))) == {
        "合同.docx",
        "其他.txt",
    }
    assert _names(
        core.metadata.query_files(
            FileQuery(modality_filters={"document_carrier_type": "scan"})
        )
    ) == {"合同.docx"}

    # Only one modality summary kind per file.
    with pytest.raises(ModalitySummaryConflict):
        core.metadata.update_modality_summary(
            library["销售.csv"].file_uid, "image", {"background_color": "灰色"}
        )
    core.metadata.clear_modality_summary(library["销售.csv"].file_uid)
    updated = core.metadata.update_modality_summary(
        library["销售.csv"].file_uid, "image", {"background_color": "灰色"}
    )
    assert updated.modality_summary_kind == "image"

    # Business date and tag validation.
    with pytest.raises(MetadataValidationError):
        core.metadata.update_semantic_metadata(
            library["其他.txt"].file_uid,
            business_start_date="2024-12-31",
            business_end_date="2024-01-01",
        )
    with pytest.raises(MetadataValidationError):
        core.metadata.update_semantic_metadata(
            library["其他.txt"].file_uid, topic_tags="not-a-list"
        )


def test_tag_tables_and_stats(core, tmp_path):
    library = _seed_metadata_library(core, tmp_path / "library")
    rows = core.db.fetchall(
        "SELECT kind, value FROM tags WHERE kind IN ('visual', 'column') ORDER BY kind, value"
    )
    assert {(row["kind"], row["value"]) for row in rows} == {
        ("visual", "人物"),
        ("visual", "会议室"),
        ("column", "销售额"),
        ("column", "地区"),
    }
    stats = core.metadata.stats()
    assert stats["total_files"] == 6
    assert stats["by_extension"]["docx"] == 2
    assert stats["by_modality"]["office"] == 3
    assert stats["by_modality"]["image"] == 1

    core.metadata.clear_modality_summary(library["会议照片.png"].file_uid)
    visual = core.db.fetchall(
        "SELECT value FROM tags WHERE kind = 'visual' ORDER BY value"
    )
    assert visual == []


# ---------------------------------------------------------------------------
# Hybrid index
# ---------------------------------------------------------------------------


def _seed_index(core: EasyOfficeCore, root: Path):
    report = core.register_inplace(_write(root / "经营报告.txt", b"report"))
    other = core.register_inplace(_write(root / "其他资料.txt", b"other"))
    core.metadata.update_semantic_metadata(report.file_uid, topic_tags=["财务"])
    core.metadata.update_semantic_metadata(other.file_uid, topic_tags=["其他"])

    report_chunks = [
        ChunkInput(
            chunk_order=0,
            plain_text="第三季度营业收入同比增长，财务分析显示利润提升。",
            page_start=2,
            page_end=2,
            bbox=(10, 20, 300, 400),
            embedding=_unit_vector(0),
        ),
        ChunkInput(
            chunk_order=1,
            plain_text="Customer revenue growth report for Q3.",
            page_start=3,
            embedding=_unit_vector(1),
        ),
        ChunkInput(
            chunk_order=2,
            structured_text="<table><tr><td>成本</td><td>100</td></tr></table>",
            chunk_type="table",
            page_start=4,
            embedding=_unit_vector(2),
        ),
    ]
    core.add_chunks(report.file_uid, report_chunks)
    core.add_chunks(
        other.file_uid,
        [ChunkInput(0, "季度营收无关内容", embedding=_unit_vector(3))],
    )
    return report, other


def test_bm25_vector_search_and_traceability(core, tmp_path):
    report, other = _seed_index(core, tmp_path / "index")

    hits = core.search_bm25("营业收入 增长", top_k=5)
    assert hits and hits[0].chunk.chunk_order == 0
    assert hits[0].source == "bm25"
    assert hits[0].file.file_uid == report.file_uid
    assert hits[0].chunk.page_start == 2
    assert hits[0].chunk.bbox == (10.0, 20.0, 300.0, 400.0)
    assert "营业收入" in hits[0].snippet

    english = core.search_bm25("revenue", top_k=5)
    assert english and english[0].chunk.chunk_order == 1

    table = core.search_bm25("成本", top_k=5)
    assert table and table[0].chunk.chunk_order == 2
    assert table[0].chunk.chunk_type == "table"
    table_record = core.index.get_chunks(report.file_uid)[2]
    assert "<table>" not in table_record.plain_text
    assert "成本" in table_record.plain_text
    assert "<table>" in table_record.structured_text

    assert core.search_bm25("", top_k=5) == []

    vector_hits = core.search_vector(_blend(0, 1), top_k=5)
    assert vector_hits[0].file.file_uid == report.file_uid
    assert vector_hits[0].chunk.chunk_order == 0
    assert vector_hits[0].source == "vector"
    assert vector_hits[0].score == pytest.approx(1.0, abs=0.02)

    filtered = core.search_vector(
        _blend(0, 1),
        file_filter=FileQuery(topic_tags=["其他"]),
        top_k=5,
    )
    assert {hit.file.file_uid for hit in filtered} == {other.file_uid}

    pages = core.index.get_chunks(report.file_uid, page_start=2, page_end=3)
    assert [chunk.chunk_order for chunk in pages] == [0, 1]

    with pytest.raises(VectorDimensionMismatch):
        core.search_vector([1.0, 0.0], top_k=3)
    with pytest.raises(VectorDimensionMismatch):
        core.add_chunks(
            report.file_uid, [ChunkInput(9, "bad-dim", embedding=[1.0, 0.0])]
        )
    with pytest.raises(ValueError):
        ChunkInput(11, "bad-range", page_start=3, page_end=2)
    with pytest.raises(ValueError):
        core.add_chunks(
            report.file_uid,
            [ChunkInput(10, "a", embedding=_unit_vector(0)), ChunkInput(10, "b", embedding=_unit_vector(0))],
        )

    replaced = core.index.replace_file_chunks(
        report.file_uid,
        [ChunkInput(0, "替换后的全新内容", embedding=_unit_vector(4))],
    )
    assert len(replaced) == 1
    assert core.search_bm25("营业收入", top_k=5) == []
    assert core.search_bm25("替换后", top_k=5)[0].chunk.plain_text == "替换后的全新内容"


def test_mirror_cache_generation_parsing_and_mapping(core, tmp_path):
    source = _write(tmp_path / "mirror" / "复杂报告.txt", b"mirror")
    record = core.register_inplace(source)
    core.add_chunks(
        record.file_uid,
        [
            ChunkInput(
                0,
                "第一行正文\n第二行正文",
                page_start=7,
                sheet_name="Sheet 1|A",
                row_start=5,
                row_end=6,
                embedding=_unit_vector(0),
            ),
            ChunkInput(
                1,
                structured_text="<table><tr><td>成本</td><td>100</td></tr></table>",
                chunk_type="table",
                page_start=8,
                embedding=_unit_vector(1),
            ),
        ],
    )

    mirror_path = core.mirror.path_for(record.file_uid)
    assert mirror_path.is_file()
    raw_lines = [line for line in mirror_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(raw_lines) == 3
    assert all(line.startswith("@@EO|") for line in raw_lines)
    parsed = [MirrorLine.parse(line) for line in raw_lines]
    assert all(item is not None for item in parsed)
    assert parsed[0].page_start == 7
    assert parsed[0].sheet_name == "Sheet 1|A"
    assert parsed[0].row_start == 5
    assert "第一行正文" in parsed[0].text
    assert parsed[2].kind == "table"
    assert "<td>" not in parsed[2].text and "成本" in parsed[2].text

    hit = core.mirror.describe_hit(parsed[0], core.metadata.get_file)
    assert hit["source_path"] == str(source.resolve())
    assert hit["page_start"] == 7
    assert hit["file_name"] == "复杂报告.txt"

    core.config.update({"storage": {"mirror_enabled": False}})
    second = core.register_inplace(_write(tmp_path / "mirror" / "无镜像.txt", b"none"))
    core.add_chunks(second.file_uid, [ChunkInput(0, "disabled", embedding=_unit_vector(2))])
    assert not core.mirror.exists(second.file_uid)

    core.config.update({"storage": {"mirror_enabled": True}})
    core.add_chunks(second.file_uid, [ChunkInput(1, "enabled", embedding=_unit_vector(3))])
    assert core.mirror.exists(second.file_uid)

    assert core.index.delete_chunks_for_file(record.file_uid) == 2
    assert not mirror_path.exists()


def test_chunk_upsert_updates_fts_vector_and_mirror(core, tmp_path):
    record = core.register_inplace(_write(tmp_path / "upsert.txt", b"upsert"))
    core.add_chunks(record.file_uid, [ChunkInput(0, "旧版苹果", embedding=_unit_vector(0))])
    assert core.search_bm25("旧版苹果", top_k=3)[0].chunk.chunk_order == 0

    core.add_chunks(record.file_uid, [ChunkInput(0, "新版香蕉", embedding=_unit_vector(1))])
    assert core.index.count_chunks(record.file_uid) == 1
    assert core.search_bm25("旧版苹果", top_k=3) == []
    assert core.search_bm25("新版香蕉", top_k=3)[0].chunk.plain_text == "新版香蕉"
    vector_hit = core.search_vector(_unit_vector(1), top_k=3)[0]
    assert vector_hit.chunk.plain_text == "新版香蕉"
    mirror_text = core.mirror.path_for(record.file_uid).read_text(encoding="utf-8")
    assert "新版香蕉" in mirror_text and "旧版苹果" not in mirror_text


def test_changed_content_clears_stale_derived_data(core, tmp_path):
    path = _write(tmp_path / "changing.txt", b"version-one")
    record = core.register_inplace(path)
    core.add_chunks(record.file_uid, [ChunkInput(0, "版本一", embedding=_unit_vector(0))])
    assert core.index.count_chunks(record.file_uid) == 1
    assert core.mirror.exists(record.file_uid)

    _write(path, b"version-two")
    refreshed = core.register_inplace(path)
    assert refreshed.file_uid == record.file_uid
    assert refreshed.content_hash == _sha(b"version-two")
    assert core.index.count_chunks(record.file_uid) == 0
    assert not core.mirror.exists(record.file_uid)


def test_plain_text_column_is_markup_free(core, tmp_path):
    record = core.register_inplace(_write(tmp_path / "markup.txt", b"markup"))
    core.add_chunks(
        record.file_uid,
        [
            ChunkInput(
                0,
                plain_text="<b>加粗正文</b>",
                structured_text="<b>加粗正文</b>",
                embedding=_unit_vector(0),
            )
        ],
    )
    chunk = core.index.get_chunks(record.file_uid)[0]
    assert chunk.plain_text == "加粗正文"
    assert chunk.structured_text == "<b>加粗正文</b>"
    assert "<b>" not in core.mirror.path_for(record.file_uid).read_text(encoding="utf-8")


def test_delete_file_cleans_chunks_mirror_and_managed_bytes(core, tmp_path):
    record = core.ingest_upload("delete-me.txt", io.BytesIO(b"delete"))
    path = Path(record.local_path)
    core.add_chunks(record.file_uid, [ChunkInput(0, "待删除", embedding=_unit_vector(0))])
    assert path.is_file() and core.mirror.exists(record.file_uid)

    assert core.delete_file(record.file_uid) is True
    assert not path.exists()
    assert not core.mirror.exists(record.file_uid)
    assert core.metadata.get_file(record.file_uid) is None
    assert core.index.count_chunks(record.file_uid) == 0


# ---------------------------------------------------------------------------
# Connectivity, WAL and hot-reloaded paths
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not REAL_SERVICES or not REAL_CONFIG.is_file(),
    reason="set EASYOFFICE_REAL_SERVICES=1 and provide config.yaml",
)
def test_real_connectivity_probe():
    manager = ConfigManager(config_path=REAL_CONFIG)
    results = manager.test_connectivity("all")
    assert results["embedding"]["status"] == "ok"
    assert results["vlm"]["status"] == "ok"
    assert results["local_parser"]["status"] in {"ok", "not_configured"}


def test_sqlite_wal_vector_extension_and_reopen(core, tmp_path):
    assert core.db.fetchone("PRAGMA journal_mode")[0].lower() == "wal"
    assert core.db.fetchone("SELECT vec_version()")[0].startswith("v")
    assert core.db.table_exists("chunk_vec")
    indexes = {
        row["name"]
        for row in core.db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert {
        "idx_files_ext_modified",
        "idx_files_ext_size",
        "idx_files_mode_modified",
        "idx_files_ext_business",
    } <= indexes

    record = core.register_inplace(_write(tmp_path / "persist.txt", b"persist"))
    core.add_chunks(record.file_uid, [ChunkInput(0, "持久化", embedding=_unit_vector(0))])
    config_path = core.config.config_path
    data_dir = core.config.data_dir
    core.close()

    reopened = EasyOfficeCore.bootstrap(config_path=config_path, data_dir=data_dir)
    try:
        assert reopened.metadata.count_files() == 1
        assert reopened.index.count_chunks(record.file_uid) == 1
        assert reopened.db.embedding_dim == DIM
    finally:
        reopened.close()


def test_hot_reload_paths_and_managed_workspace(core, tmp_path):
    new_db = tmp_path / "new-index" / "knowledge.db"
    new_managed = tmp_path / "new-managed"
    new_mirror = tmp_path / "new-mirror"

    core.config.update(
        {
            "storage": {
                "index_db_path": str(new_db),
                "managed_workspace": str(new_managed),
                "mirror_cache_dir": str(new_mirror),
            }
        }
    )
    assert core.db.path == new_db.resolve()
    assert new_db.is_file()
    assert core.storage.managed_workspace == new_managed.resolve()
    assert core.mirror.root == new_mirror.resolve()
    assert core.metadata.count_files() == 0  # no data migration by design

    record = core.ingest_upload("hot.txt", io.BytesIO(b"hot"))
    assert str(new_managed.resolve()) in record.local_path
    core.add_chunks(record.file_uid, [ChunkInput(0, "热重载", embedding=_unit_vector(0))])
    assert core.mirror.path_for(record.file_uid).is_file()
    assert str(new_mirror.resolve()) in str(core.mirror.path_for(record.file_uid))


def test_unsupported_driver_is_rejected(core):
    with pytest.raises(Exception):
        core.config.update({"storage": {"vector_store_driver": "elasticsearch"}})
    with pytest.raises(Exception):
        core.config.update({"storage": {"object_store_driver": "minio"}})
