"""Step 2 business walkthrough: index real office documents end to end.

Run with the real documents and real model services configured in
``config.yaml``:

    .venv\\Scripts\\python.exe tests\\step_02_business_walkthrough.py data
    .venv\\Scripts\\python.exe tests\\step_02_business_walkthrough.py data --query 功率预测

The script prints one row per document (format, carrier type, pages, chunks,
tables, images kept/filtered, formulas, vectors), then shows the backfilled
"office structure summary", a complex table's HTML, a real VLM image description,
the mirror cache and BM25/vector search results. Exit code 0 means every office
document was ingested; otherwise it is 1 and the failures are listed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from easy_office.core import EasyOfficeCore  # noqa: E402
from easy_office.models.services import build_embedding_client  # noqa: E402
from easy_office.parsing.pipeline import OFFICE_EXTENSIONS  # noqa: E402

SUPPORTED = {f".{extension}" for extension in OFFICE_EXTENSIONS}


def _is_runtime_path(path: Path, data_dir: Path) -> bool:
    for root in (data_dir, ROOT / "runtime", ROOT / "managed_src_files", ROOT / "models"):
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Step 2 office ingestion walkthrough")
    parser.add_argument(
        "docs",
        nargs="?",
        default=str(ROOT / "data"),
        help="folder containing the real test documents (default: data)",
    )
    parser.add_argument(
        "--data-dir",
        default=str(ROOT / "runtime"),
        help="rebuildable runtime directory (default: <project>/runtime)",
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "config.yaml"),
        help="real configuration file (default: <project>/config.yaml)",
    )
    parser.add_argument(
        "--query",
        default="",
        help="BM25/vector query; defaults to text taken from the first chunk",
    )
    parser.add_argument("--limit", type=int, default=0, help="limit the number of files")
    return parser.parse_args()


def _print_summary_row(result) -> None:
    print(
        f"  {result.source_format:<5} {result.carrier_type:<8} "
        f"pages={result.page_count:<3} chunks={result.chunk_count:<4} "
        f"tables={result.table_count:<2} images={result.image_count}/"
        f"{result.image_count + result.filtered_image_count:<2} "
        f"formulas={result.formula_count:<2} vectors={result.embedded_chunk_count:<4} "
        f"{result.elapsed_seconds:6.2f}s"
    )


def main() -> int:
    args = _parse_args()
    if not args.docs:
        print("[失败] 请提供测试文档目录，或设置 EASYOFFICE_STEP2_DOCS。")
        return 1
    docs_dir = Path(args.docs).expanduser().resolve()
    if not docs_dir.is_dir():
        print(f"[失败] 目录不存在：{docs_dir}")
        return 1
    data_dir = Path(args.data_dir).resolve()
    files = [
        path
        for path in sorted(docs_dir.rglob("*"))
        if path.is_file()
        and path.suffix.lower() in SUPPORTED
        and not path.name.startswith("~$")
        and not _is_runtime_path(path, data_dir)
    ]
    if args.limit > 0:
        files = files[: args.limit]
    if not files:
        print(f"[失败] 目录中没有办公文档：{docs_dir}")
        return 1

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.is_file():
        print(f"[失败] 缺少真实模型配置文件：{config_path}")
        return 1

    print("=" * 88)
    print("步骤二业务演示：多源版面办公文档解析、表格还原与结构化切片")
    print(f"测试目录：{docs_dir}")
    print(f"配置文件：{config_path}")
    print(f"运行目录：{Path(args.data_dir).resolve()}")
    print(f"待处理文件：{len(files)} 个")
    print("=" * 88)

    core = EasyOfficeCore.bootstrap(config_path=config_path, data_dir=args.data_dir)
    embedding_client = build_embedding_client(core.config.get().embedding)
    vlm_config = core.config.get().vlm
    if embedding_client is None or not vlm_config.base_url or not vlm_config.model_name:
        core.close()
        print("[失败] 请在项目根 config.yaml 中配置真实 Embedding 与 VLM 服务。")
        return 1
    results = []
    failures: list[tuple[Path, str]] = []
    try:
        for index, path in enumerate(files, start=1):
            print(f"\n[{index}/{len(files)}] {path.name}")
            try:
                result = core.office.ingest_path(path)
            except Exception as exc:  # noqa: BLE001 - demo reports every failure
                print(f"  [失败] {type(exc).__name__}: {exc}")
                failures.append((path, str(exc)))
                continue
            results.append(result)
            _print_summary_row(result)
            reasons = result.image_stats.get("filter_reasons") or {}
            if reasons:
                print("  图片过滤明细：" + ", ".join(f"{key}={value}" for key, value in reasons.items()))
            if result.warnings:
                for warning in result.warnings:
                    print(f"  [提示] {warning}")

        print("\n" + "=" * 88)
        print("一、回填的“办公文档专属结构摘要”")
        print("=" * 88)
        for result in results:
            print(f"\n{result.file_name}")
            print(json.dumps(result.summary, ensure_ascii=False, indent=2))

        if results:
            sample_result = results[0]
            sample_chunks = core.index.get_chunks(sample_result.file_uid)
            for result in results:
                candidate = core.index.get_chunks(result.file_uid)
                if any(chunk.chunk_type == "image" for chunk in candidate):
                    sample_result, sample_chunks = result, candidate
                    if any(chunk.chunk_type == "table" for chunk in candidate):
                        break
            chunks = sample_chunks

            print("\n" + "=" * 88)
            print("二、复杂表格 HTML / 简单表格 Markdown 还原样例")
            print("=" * 88)
            table_chunks = [chunk for chunk in chunks if chunk.chunk_type == "table"]
            html_tables = [
                chunk for chunk in table_chunks if chunk.metadata.get("table_format") == "html"
            ]
            sample = (html_tables or table_chunks or [None])[0]
            if sample is not None:
                print(
                    f"文件：{sample_result.file_name}  页码：{sample.page_start}  "
                    f"格式：{sample.metadata.get('table_format')}  "
                    f"复杂度：{sample.metadata.get('table_complexity')}"
                )
                print(sample.structured_text[:2000])
            else:
                print("样例文件没有解析出表格。")

            print("\n" + "=" * 88)
            print("三、内嵌图片过滤与视觉描述样例")
            print("=" * 88)
            image_chunks = [chunk for chunk in chunks if chunk.chunk_type == "image"]
            if image_chunks:
                chunk = image_chunks[0]
                print(
                    f"文件：{sample_result.file_name}  页码：{chunk.page_start}  "
                    f"裁剪图：{chunk.image_path}"
                )
                print(f"描述来源：{chunk.metadata.get('description_source')}")
                print(f"视觉标签：{chunk.metadata.get('visual_tags')}")
                print(chunk.plain_text[:1500])
            else:
                print("样例文件没有保留下来的实质性图片。")

            print("\n" + "=" * 88)
            print("四、纯文本伴生镜像缓存")
            print("=" * 88)
            mirror_path = core.mirror.path_for(sample_result.file_uid)
            print(f"路径：{mirror_path}")
            if mirror_path.is_file():
                with open(mirror_path, "r", encoding="utf-8", errors="replace") as handle:
                    for _ in range(5):
                        line = handle.readline()
                        if not line:
                            break
                        print(line.rstrip("\n"))
            else:
                print("镜像文件未生成（检查 storage.mirror_enabled）。")

            print("\n" + "=" * 88)
            print("五、检索验证：BM25 + 向量")
            print("=" * 88)
            query = args.query.strip()
            if not query:
                for chunk in chunks:
                    body = chunk.plain_text
                    for line in body.splitlines():
                        if line.startswith("["):
                            continue
                        if len(line.strip()) >= 6:
                            query = line.strip()[:20]
                            break
                    if query:
                        break
            query = query or "文档"
            print(f"查询：{query}")
            hits = core.search_bm25(query, top_k=5)
            print(f"BM25 命中：{len(hits)}")
            for hit in hits:
                print(
                    f"  #{hit.rank} {hit.file.file_name} p.{hit.chunk.page_start} "
                    f"[{hit.chunk.chunk_type}] {hit.snippet[:120]}"
                )

            embedding_client = build_embedding_client(core.config.get().embedding)
            if embedding_client is not None:
                vector = embedding_client.embed_query(query)
                vector_hits = core.search_vector(vector, top_k=5)
                print(f"向量命中：{len(vector_hits)}")
                for hit in vector_hits:
                    print(
                        f"  #{hit.rank} {hit.file.file_name} p.{hit.chunk.page_start} "
                        f"score={hit.score:.4f} {hit.snippet[:120]}"
                    )
            else:
                print("向量检索：未配置 embedding 服务，已跳过（文本索引仍可用）。")

        print("\n" + "=" * 88)
        if failures:
            print(f"演示结束：{len(results)} 个成功，{len(failures)} 个失败。")
            for path, error in failures:
                print(f"  [失败] {path.name}: {error}")
            return 1
        print(f"演示完成：{len(results)} 个文档全部入库。")
        return 0
    finally:
        core.close()


if __name__ == "__main__":
    raise SystemExit(main())
