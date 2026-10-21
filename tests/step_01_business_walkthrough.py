"""Step 1 business walkthrough using real documents and real model services.

The script tells one continuous user story with the files the user actually
provided: configure the assistant, register a real host file, receive a real
browser upload, run the real Step 2 parser (Docling/RapidOCR/VLM), backfill
metadata, derive real table/image records from the parsed output, run BM25 and
real Embedding vector search, inspect the companion mirror, and archive a file
while keeping the database path in sync.

No file bytes or vectors are fabricated. The only generated files are a CSV
and a PNG copied from the parsed real document, used to demonstrate the table
and image modality-summary APIs.

Run from the project root::

    .venv\\Scripts\\python.exe tests\\step_01_business_walkthrough.py data

Options:
    data                  directory containing real DOCX/PDF/PPTX/DOC/PPT files
    --data-dir PATH       runtime data directory (default: temporary)
    --inplace PATH        real file registered in place (default: first DOCX)
    --upload PATH         real file streamed through managed upload (default: first PDF)
    --query TEXT          real search query (default: 功率预测)
    --keep                keep the temporary runtime directory

The script requires the project-root ``config.yaml`` with a configured Embedding service
and a configured VLM (the same ones used by Step 2). It exits non-zero if the
services or the Docling weights are missing.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from easy_office.config.manager import ConfigValidationError
from easy_office.core import EasyOfficeCore
from easy_office.metadata.models import FileQuery
from easy_office.models.services import build_embedding_client

SUPPORTED = {".docx", ".pdf", ".pptx", ".doc", ".ppt"}


def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  [通过] {message}")


def describe_record(record) -> None:
    print(
        f"  文件：{record.file_name}\n"
        f"  存储模式：{record.storage_mode}  大小：{record.size_bytes} 字节  "
        f"MIME：{record.mime_type}\n"
        f"  SHA-256：{record.content_hash[:16]}…\n"
        f"  本地路径：{record.local_path}"
    )


def _pick_document(docs_dir: Path, extension: str, exclude: set[Path] | None = None) -> Path:
    exclude = exclude or set()
    for path in sorted(docs_dir.rglob(f"*{extension}")):
        if path.is_file() and not path.name.startswith("~$") and path not in exclude:
            return path
    raise SystemExit(f"未在 {docs_dir} 找到真实 {extension} 文档")


def _pick_digital_pdf(docs_dir: Path, exclude: set[Path] | None = None) -> Path:
    """Prefer a PDF with a real native-text layer for the fast business path."""
    exclude = exclude or set()
    pdfs = [
        path
        for path in sorted(docs_dir.rglob("*.pdf"))
        if path.is_file() and not path.name.startswith("~$") and path not in exclude
    ]
    if not pdfs:
        raise SystemExit(f"未在 {docs_dir} 找到真实 PDF 文档")
    try:
        from easy_office.parsing.pdf_probe import probe_pdf

        for path in pdfs:
            if any(not page.is_scanned for page in probe_pdf(path)):
                return path
    except Exception:
        pass
    return pdfs[0]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Step 1 真实业务链路演示")
    parser.add_argument("docs", nargs="?", default=str(PROJECT_ROOT / "data"),
                        help="真实文档目录（默认 data）")
    parser.add_argument("--data-dir", default="", help="运行数据目录（默认临时目录）")
    parser.add_argument("--inplace", default="", help="原地引用模式使用的真实文件")
    parser.add_argument("--upload", default="", help="托管上传模式使用的真实文件")
    parser.add_argument("--query", default="功率预测", help="真实检索查询")
    parser.add_argument("--keep", action="store_true", help="保留临时数据目录")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    docs_dir = Path(args.docs).expanduser().resolve()
    config_path = PROJECT_ROOT / "config.yaml"
    weights_dir = PROJECT_ROOT / "models"
    if not config_path.is_file():
        print(f"[失败] 缺少真实模型配置文件：{config_path}", file=sys.stderr)
        return 1
    if not weights_dir.is_dir() or not any(weights_dir.iterdir()):
        print(f"[失败] 缺少 Docling 权重目录：{weights_dir}", file=sys.stderr)
        return 1
    if not docs_dir.is_dir():
        print(f"[失败] 文档目录不存在：{docs_dir}", file=sys.stderr)
        return 1

    root = Path(tempfile.mkdtemp(prefix="easyoffice-step01-real-"))
    host_dir = root / "宿主文件夹"
    host_dir.mkdir(parents=True)
    runtime_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    inplace_source = (
        Path(args.inplace).expanduser().resolve()
        if args.inplace
        else _pick_document(docs_dir, ".docx")
    )
    upload_source = (
        Path(args.upload).expanduser().resolve()
        if args.upload
        else _pick_digital_pdf(docs_dir, exclude={inplace_source})
    )
    if upload_source == inplace_source:
        raise SystemExit("--inplace 与 --upload 不能是同一文件")

    core = EasyOfficeCore.bootstrap(
        config_path=config_path,
        data_dir=runtime_dir,
        config_patch={
            "local_parser": {"weights_dir": str(weights_dir)},
            "storage": {"managed_workspace": str(root / "managed_src_files")},
        },
        persist_patch=False,
    )

    try:
        banner("步骤 1：加载真实配置与本地存储")
        paths = core.config.paths()
        print(f"  真实配置：{config_path}")
        print(f"  运行目录：{paths.data_dir}")
        print(f"  索引数据库：{paths.index_db_path}")
        print(f"  托管工作区：{paths.managed_workspace}")
        print(f"  伴生镜像目录：{paths.mirror_dir}")
        print(f"  资产缓存目录：{paths.asset_cache_dir}")
        print(f"  原地引用文档：{inplace_source}")
        print(f"  托管上传文档：{upload_source}")
        check(paths.index_db_path.is_file(), "真实配置已加载，SQLite 索引数据库已创建")

        embedding_client = build_embedding_client(core.config.get().embedding)
        vlm_config = core.config.get().vlm
        check(embedding_client is not None, "真实 Embedding 服务已配置")
        check(bool(vlm_config.base_url and vlm_config.model_name), "真实 VLM 服务已配置")

        banner("步骤 2：在线修改 LLM 温度，观察热重载与真实密钥脱敏")
        observed = []
        core.config.subscribe(lambda cfg: observed.append(cfg.llm.temperature))
        original = core.config.get().llm.temperature
        updated = 0.25 if abs(original - 0.25) > 1e-9 else 0.3
        core.config.set("llm.temperature", updated, persist=False)
        check(
            observed == [updated],
            f"配置变更已触发热重载通知（temperature {original} → {updated}）",
        )
        public_llm = core.config.public_config()["llm"]
        if core.config.get().llm.api_key.get_secret_value():
            check(public_llm["api_key"] == "********", "真实 LLM 密钥在公开视图中已脱敏")
        else:
            print("  LLM 未配置密钥，跳过脱敏断言（Embedding/VLM 密钥仍按同一机制处理）")
        core.config.set("llm.temperature", original, persist=False)

        banner("步骤 3：宿主机原地引用模式登记真实文档")
        local_copy = host_dir / inplace_source.name
        shutil.copy2(inplace_source, local_copy)
        record = core.register_inplace(local_copy)
        describe_record(record)
        check(record.storage_mode == "inplace", "真实文档以宿主机原地引用模式登记")
        check(record.content_hash and len(record.content_hash) == 64, "SHA-256 已计算")
        check(record.size_bytes == local_copy.stat().st_size, "文件大小采集准确")
        check(
            not (core.storage.managed_workspace / record.file_uid).exists(),
            "原地引用模式没有复制任何文件",
        )

        banner("步骤 4：跨端上传托管归档模式接收真实文件流")
        with open(upload_source, "rb") as handle:
            uploaded = core.ingest_upload(upload_source.name, handle)
        describe_record(uploaded)
        check(uploaded.storage_mode == "managed", "真实文件流进入托管归档模式")
        check(
            str(core.storage.managed_workspace) in uploaded.local_path,
            "托管文件物理存放在托管工作区",
        )
        check(
            Path(uploaded.local_path).read_bytes() == upload_source.read_bytes(),
            "上传字节与真实源文件完全一致",
        )

        banner("步骤 5：真实解析、切片、Embedding 与办公结构摘要")
        inplace_result = core.office.ingest(record.file_uid)
        upload_result = core.office.ingest(uploaded.file_uid)
        print(
            f"  原地文档：{inplace_result.source_format}  {inplace_result.page_count} 页  "
            f"{inplace_result.chunk_count} 切片  {inplace_result.embedded_chunk_count} 向量"
        )
        print(
            f"  上传文档：{upload_result.source_format}  {upload_result.page_count} 页  "
            f"{upload_result.chunk_count} 切片  {upload_result.embedded_chunk_count} 向量"
        )
        check(inplace_result.chunk_count > 0, "真实文档已产生可检索切片")
        check(
            inplace_result.embedded_chunk_count == inplace_result.chunk_count,
            "所有切片均由真实 Embedding 服务向量化",
        )
        check(
            upload_result.embedded_chunk_count == upload_result.chunk_count,
            "上传文档的所有切片均已向量化",
        )

        banner("步骤 6：回填真实语义元数据与三类模态专属摘要")
        first_text = next(
            (chunk.plain_text for chunk in core.index.get_chunks(record.file_uid)
             if chunk.chunk_type == "text" and chunk.plain_text.strip()),
            "",
        )
        summary_text = " ".join(first_text.split())[:120]
        core.metadata.update_semantic_metadata(
            record.file_uid,
            topic_tags=["新能源", "功率预测"],
            summary=summary_text or "真实文档解析内容",
            entities=["风电", "光伏"],
            keywords=["功率预测", "人工智能"],
            business_start_date="2021-01-01",
            business_end_date="2024-12-31",
        )
        refreshed = core.metadata.get_file(record.file_uid)
        check(refreshed and refreshed.topic_tags == ["新能源", "功率预测"], "真实语义标签已回填")
        check(
            refreshed and refreshed.modality_summary_kind == "office",
            "办公文档专属结构摘要已由真实解析结果回填",
        )

        # Derive a real table record from the parsed document (no fabricated rows).
        table_chunks = [
            chunk for chunk in core.index.get_chunks(record.file_uid)
            if chunk.chunk_type == "table"
        ]
        if table_chunks:
            chunk = table_chunks[0]
            rows = []
            for line in chunk.plain_text.splitlines():
                if "|" not in line or line.startswith("[") or line.startswith("表头"):
                    continue
                values = [value.strip() for value in line.split("|")]
                if not any(values):
                    continue
                rows.append(values)
            if len(rows) >= 2:
                csv_path = host_dir / "真实表格导出.csv"
                with open(csv_path, "w", encoding="utf-8", newline="") as handle:
                    csv.writer(handle).writerows(rows)
                table_record = core.register_inplace(csv_path)
                table_summary = {
                    "column_names": rows[0],
                    "row_count": max(0, len(rows) - 1),
                    "derived_from": record.file_name,
                }
                core.metadata.update_modality_summary(
                    table_record.file_uid, "table", table_summary
                )
                check(
                    core.metadata.get_file(table_record.file_uid).modality_summary_kind == "table",
                    "表格专属结构摘要已用真实解析表格回填",
                )

        # Derive a real image record from a retained, VLM-described crop.
        image_chunks = [
            chunk for chunk in core.index.get_chunks(record.file_uid)
            if chunk.chunk_type == "image" and chunk.image_path
        ]
        if image_chunks:
            chunk = image_chunks[0]
            image_path = Path(chunk.image_path)
            if image_path.is_file():
                image_copy = host_dir / "真实内嵌图片.png"
                shutil.copy2(image_path, image_copy)
                image_record = core.register_inplace(image_copy)
                image_summary = {
                    "width": chunk.metadata.get("image_width"),
                    "height": chunk.metadata.get("image_height"),
                    "visual_tags": chunk.metadata.get("visual_tags") or [],
                    "description": chunk.plain_text.split("图片描述：", 1)[-1][:300],
                    "derived_from": record.file_name,
                }
                core.metadata.update_modality_summary(
                    image_record.file_uid, "image", image_summary
                )
                check(
                    core.metadata.get_file(image_record.file_uid).modality_summary_kind == "image",
                    "图像专属结构摘要已用真实 VLM 描述回填",
                )

        banner("步骤 7：多条件复合初筛")
        matched = core.metadata.query_files(
            FileQuery(
                topic_tags=["新能源"],
                extensions=[inplace_result.source_format],
                modality_kind="office",
            )
        )
        print(f"  命中：{[item.file_name for item in matched]}")
        check(
            any(item.file_uid == record.file_uid for item in matched),
            "主题 + 扩展名 + 模态类型复合筛选命中真实文档",
        )

        banner("步骤 8：真实 BM25 与真实 Embedding 向量检索")
        bm25_hits = core.search_bm25(args.query, top_k=5)
        print(f"  BM25 查询：{args.query}")
        for hit in bm25_hits:
            print(
                f"    第{hit.rank}名 得分={hit.score:.4f} {hit.file.file_name} "
                f"第{hit.chunk.page_start}页 [{hit.chunk.chunk_type}] {hit.snippet[:80]}"
            )
        check(bool(bm25_hits), "BM25 对真实文档返回检索结果")
        query_vector = embedding_client.embed_query(args.query)
        vector_hits = core.search_vector(query_vector, top_k=5)
        print("  向量检索结果：")
        for hit in vector_hits:
            print(
                f"    第{hit.rank}名 余弦相似度={hit.score:.4f} {hit.file.file_name} "
                f"第{hit.chunk.page_start}页 {hit.snippet[:80]}"
            )
        check(bool(vector_hits), "真实 Embedding 向量检索返回结果")
        check(len(query_vector) == core.config.get().embedding.embedding_dim,
              "查询向量维度与配置一致")

        banner("步骤 9：纯文本伴生镜像缓存")
        mirror_path = core.mirror.path_for(record.file_uid)
        print(f"  镜像文件：{mirror_path}")
        for line in mirror_path.read_text(encoding="utf-8").splitlines()[:2]:
            print(f"  {line}")
        check(mirror_path.is_file(), "真实文档的伴生镜像已生成")
        check(not any("<td>" in line for line in mirror_path.read_text(encoding="utf-8").splitlines()),
              "表格 HTML 标签未写入纯文本镜像")

        banner("步骤 10：文件归档移动与 SHA-256 路径同步")
        archive_dir = host_dir / "归档" / "新能源"
        archive_dir.mkdir(parents=True)
        archived_path = archive_dir / local_copy.name
        old_hash = refreshed.content_hash if refreshed else ""
        shutil.move(str(local_copy), str(archived_path))
        archived = core.sync_moved_path(record.file_uid, archived_path)
        check(archived.file_uid == record.file_uid, "归档后文件唯一标识保持不变")
        check(archived.content_hash == old_hash, "内容哈希保持不变")
        check(archived.local_path == str(archived_path.resolve()), "数据库路径已原地同步")

        banner("步骤 11：非法配置拦截与最终脱敏视图")
        try:
            core.config.update({"llm": {"temperature": 3.5}}, persist=False)
            raise AssertionError("非法温度没有被拦截")
        except ConfigValidationError as exc:
            print(f"  已拦截：{exc.errors[0]}")
            check(True, "非法配置被强类型校验拦截")
        check(
            abs(core.config.get().llm.temperature - original) < 1e-9,
            "被拒绝的配置没有污染运行中的配置中心",
        )

        banner("演示完成（真实文档 + 真实模型）")
        print("  已覆盖：真实配置热更新与脱敏、双模式存储、真实解析与向量化、")
        print("           两层元数据、三类模态摘要、复合筛选、BM25/向量检索、")
        print("           伴生镜像、SHA-256 路径同步。")
        if args.keep:
            print(f"\n  工作目录已保留：{root}")
        return 0
    finally:
        core.close()
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\n[失败] {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"\n[异常] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
