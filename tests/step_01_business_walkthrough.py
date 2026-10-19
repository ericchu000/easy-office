"""Step 1 business walkthrough (human-review tier).

This script tells one continuous story from a user's perspective: configure the
assistant, register a host folder file, receive a browser upload, backfill the
two metadata layers, filter files, index chunks, search them, inspect the
plain-text mirror, and finally archive a file while keeping metadata in sync.

Run from the project root::

    .venv\\Scripts\\python.exe tests\\step_01_business_walkthrough.py

Add ``--keep`` to keep the temporary working directory and inspect the database
and mirror files afterwards.
"""

from __future__ import annotations

import argparse
import io
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
from easy_office.indexing.base import ChunkInput
from easy_office.metadata.models import FileQuery


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Step 1 业务链路演示")
    parser.add_argument("--keep", action="store_true", help="保留临时工作目录")
    args = parser.parse_args()

    try:  # Make Chinese output readable in Windows terminals.
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    root = Path(tempfile.mkdtemp(prefix="easyoffice-step01-"))
    host_dir = root / "宿主文件夹"
    host_dir.mkdir(parents=True)
    core = EasyOfficeCore.bootstrap(
        config_path=root / "config.yaml",
        data_dir=root / "data",
        config_patch={"embedding": {"embedding_dim": 8}},
    )

    try:
        banner("步骤 1：初始化配置中心与本地存储")
        paths = core.config.paths()
        print(f"  数据目录：{paths.data_dir}")
        print(f"  配置文件：{paths.config_path}")
        print(f"  索引数据库：{paths.index_db_path}")
        print(f"  托管工作区：{paths.managed_workspace}")
        print(f"  伴生镜像目录：{paths.mirror_dir}")
        check(paths.config_path.is_file(), "默认配置文件已自动生成")
        check(paths.index_db_path.is_file(), "SQLite 索引数据库已创建")

        banner("步骤 2：在线修改 LLM 配置，观察热重载与密钥脱敏")
        observed = []
        core.config.subscribe(lambda cfg: observed.append(cfg.llm.model_name))
        core.config.update(
            {
                "llm": {
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model_name": "qwen2.5-7b-instruct",
                    "api_key": "sk-demo-secret",
                    "temperature": 0.3,
                }
            }
        )
        check(observed == ["qwen2.5-7b-instruct"], "配置变更已触发热重载通知")
        public_llm = core.config.public_config()["llm"]
        print("  脱敏后的 LLM 配置：")
        print(json.dumps(public_llm, ensure_ascii=False, indent=2))
        check(public_llm["api_key"] == "********", "API 密钥已脱敏展示")
        check(
            "sk-demo-secret" in paths.config_path.read_text(encoding="utf-8"),
            "真实密钥已安全持久化到本地 YAML",
        )

        banner("步骤 3：宿主机原地引用模式注册存量文件")
        report_path = host_dir / "季度经营分析.docx"
        report_path.write_text(
            "第三季度营业收入同比增长百分之二十，毛利率提升。", encoding="utf-8"
        )
        report = core.register_inplace(report_path)
        describe_record(report)
        check(report.storage_mode == "inplace", "文件以宿主机原地引用模式登记")
        check(report.content_hash and len(report.content_hash) == 64, "SHA-256 已计算")
        check(report.size_bytes == report_path.stat().st_size, "文件大小采集准确")
        check(not (core.storage.managed_workspace / report.file_uid).exists(),
              "原地引用模式没有复制任何文件")

        banner("步骤 4：跨端上传托管归档模式接收浏览器上传")
        csv_bytes = "姓名,电话,区域\n张三,13800000000,华东\n".encode("utf-8")
        customers = core.ingest_upload(
            "客户回访名单.csv",
            io.BytesIO(csv_bytes),
            fs_created_at="2024-06-01T09:00:00+08:00",
            fs_modified_at="2024-06-02T10:00:00+08:00",
        )
        photo = core.ingest_upload("会议照片.png", io.BytesIO(b"\x89PNG\r\n\x1a\nfake"))
        describe_record(customers)
        check(customers.storage_mode == "managed", "上传文件进入托管归档模式")
        check(
            str(core.storage.managed_workspace) in customers.local_path,
            "托管文件物理存放在托管工作区",
        )
        check(Path(customers.local_path).read_bytes() == csv_bytes, "上传字节完整落盘")
        check(photo.extension == "png", "图片 MIME/扩展名识别正确")

        banner("步骤 5：回填第二层语义元数据与三类模态专属结构摘要")
        core.metadata.update_semantic_metadata(
            report.file_uid,
            topic_tags=["财务", "年度报告"],
            summary="季度经营分析，营业收入与毛利率保持增长。",
            entities=["甲公司", "财务部"],
            keywords=["营业收入", "毛利率", "增长"],
            business_start_date="2024-01-01",
            business_end_date="2024-12-31",
        )
        core.metadata.update_modality_summary(
            report.file_uid,
            "office",
            {
                "document_carrier_type": "digital",
                "page_count": 3,
                "has_tables": True,
                "has_images": True,
            },
        )
        core.metadata.update_semantic_metadata(
            customers.file_uid,
            topic_tags=["财务", "业务数据表"],
            summary="客户回访名单，包含联系方式与区域信息。",
            keywords=["客户", "回访"],
            business_start_date="2024-06-01",
            business_end_date="2024-06-30",
        )
        core.metadata.update_modality_summary(
            customers.file_uid,
            "table",
            {"column_names": ["姓名", "电话", "区域"], "row_count": 1},
        )
        core.metadata.update_semantic_metadata(
            photo.file_uid,
            topic_tags=["财务", "会议"],
            summary="财务会议白板照片。",
        )
        core.metadata.update_modality_summary(
            photo.file_uid,
            "image",
            {
                "width": 1920,
                "height": 1080,
                "background_color": "白色",
                "scene_category": "会议白板",
                "visual_tags": ["人物", "白板", "会议室"],
            },
        )
        refreshed = core.metadata.get_file(report.file_uid)
        check(refreshed and refreshed.topic_tags == ["财务", "年度报告"], "语义标签已回填")
        check(
            refreshed and refreshed.modality_summary_kind == "office",
            "办公文档专属结构摘要已挂接",
        )
        check(
            core.metadata.get_file(customers.file_uid).modality_summary_kind == "table",
            "表格专属结构摘要已挂接",
        )
        check(
            core.metadata.get_file(photo.file_uid).modality_summary_kind == "image",
            "图像专属结构摘要已挂接",
        )

        banner("步骤 6：多条件复合初筛")
        finance_docx = core.metadata.query_files(
            FileQuery(
                topic_tags=["财务", "年度报告"],
                require_all_topic_tags=True,
                extensions=["docx"],
                modality_filters={"has_tables": True},
                business_start_from="2024-06-01",
                business_end_to="2024-06-30",
            )
        )
        print("  条件：主题同时包含财务+年度报告、扩展名 docx、含表格、业务时间与 2024-06 重叠")
        print(f"  命中：{[item.file_name for item in finance_docx]}")
        check([item.file_name for item in finance_docx] == ["季度经营分析.docx"],
              "办公文档复合筛选准确")

        white_images = core.metadata.query_files(
            FileQuery(modality_kind="image", modality_filters={"background_color": "白色"})
        )
        check([item.file_name for item in white_images] == ["会议照片.png"],
              "按图像背景颜色筛选准确")

        column_tables = core.metadata.query_files(
            FileQuery(modality_filters={"column_names": "区域"})
        )
        check([item.file_name for item in column_tables] == ["客户回访名单.csv"],
              "按表头列名筛选准确")

        banner("步骤 7：切片入库、BM25 全文检索与向量语义检索")
        print("  说明：本步骤不调用真实嵌入模型，使用 8 维模拟向量验证存储与检索链路。")
        core.add_chunks(
            report.file_uid,
            [
                ChunkInput(
                    0,
                    "第三季度营业收入同比增长百分之二十，毛利率提升。",
                    page_start=1,
                    page_end=1,
                    bbox=(40, 60, 520, 760),
                    embedding=[1, 0, 0, 0, 0, 0, 0, 0],
                ),
                ChunkInput(
                    1,
                    "第四季度营业收入预计保持稳定。",
                    page_start=2,
                    embedding=[0, 1, 0, 0, 0, 0, 0, 0],
                ),
                ChunkInput(
                    2,
                    structured_text="<table><tr><td>项目</td><td>金额</td></tr>"
                    "<tr><td>营业收入</td><td>1200</td></tr></table>",
                    chunk_type="table",
                    page_start=3,
                    embedding=[0, 0, 1, 0, 0, 0, 0, 0],
                ),
            ],
        )
        core.add_chunks(
            customers.file_uid,
            [ChunkInput(0, "客户回访名单：姓名、电话、区域。", embedding=[0, 0, 0, 1, 0, 0, 0, 0])],
        )
        core.add_chunks(
            photo.file_uid,
            [ChunkInput(0, "会议白板照片，白色背景，人物讨论财务指标。", embedding=[0, 0, 0, 0, 1, 0, 0, 0])],
        )

        bm25_hits = core.search_bm25("营业收入 增长", top_k=3)
        for hit in bm25_hits:
            print(
                f"  BM25 第{hit.rank}名 得分={hit.score:.4f} "
                f"{hit.file.file_name} 第{hit.chunk.page_start}页：{hit.snippet}"
            )
        check(
            bm25_hits and bm25_hits[0].file.file_name == "季度经营分析.docx",
            "BM25 中文检索命中经营分析文档",
        )

        vector_hits = core.search_vector(
            [0.98, 0.02, 0, 0, 0, 0, 0, 0], top_k=3
        )
        for hit in vector_hits:
            print(
                f"  向量 第{hit.rank}名 余弦相似度={hit.score:.4f} "
                f"{hit.file.file_name}：{hit.snippet}"
            )
        check(
            vector_hits and vector_hits[0].file.file_name == "季度经营分析.docx",
            "向量近邻检索命中目标文档",
        )

        image_only = core.search_vector(
            [0, 0, 0, 0.2, 0.98, 0, 0, 0],
            file_filter=FileQuery(extensions=["png"]),
            top_k=3,
        )
        check(
            image_only and image_only[0].file.file_name == "会议照片.png",
            "带元数据过滤的向量检索只返回图片",
        )

        banner("步骤 8：纯文本伴生镜像缓存")
        mirror_path = core.mirror.path_for(report.file_uid)
        print(f"  镜像文件：{mirror_path}")
        print("  前两行（每行带文件标识与页码前缀）：")
        for line in mirror_path.read_text(encoding="utf-8").splitlines()[:2]:
            print(f"  {line}")
        check(mirror_path.is_file(), "伴生镜像文件已生成")
        mirror_lines = core.mirror.read_lines(report.file_uid)
        check(
            mirror_lines and mirror_lines[0].page_start == 1,
            "镜像行可解析回页码定位",
        )
        check(
            not any("<td>" in item.text for item in mirror_lines),
            "表格 HTML 标签未写入纯文本镜像",
        )

        banner("步骤 9：文件归档移动与 SHA-256 路径同步")
        archive_dir = host_dir / "归档" / "财务"
        archive_dir.mkdir(parents=True)
        archived_path = archive_dir / "季度经营分析.docx"
        old_hash = refreshed.content_hash if refreshed else ""
        shutil.move(str(report_path), str(archived_path))
        archived = core.sync_moved_path(report.file_uid, archived_path)
        print(f"  新路径：{archived.local_path}")
        check(archived.file_uid == report.file_uid, "归档后文件唯一标识保持不变")
        check(archived.content_hash == old_hash, "内容哈希保持不变")
        check(archived.local_path == str(archived_path.resolve()), "数据库路径已原地同步")

        banner("步骤 10：非法配置拦截与最终脱敏视图")
        try:
            core.config.update({"llm": {"temperature": 3.5}})
            raise AssertionError("非法温度没有被拦截")
        except ConfigValidationError as exc:
            print(f"  已拦截：{exc.errors[0]}")
            check(True, "非法配置被强类型校验拦截")
        check(
            core.config.get().llm.temperature == 0.3,
            "被拒绝的配置没有污染运行中的配置中心",
        )

        banner("演示完成")
        print("  已覆盖：配置热更新与脱敏、双模式存储、两层元数据、三类模态摘要、")
        print("           复合筛选、BM25/向量检索、伴生镜像、哈希路径同步。")
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
    except Exception as exc:  # pragma: no cover - interactive script
        print(f"\n[异常] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
