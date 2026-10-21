"""Download Docling model weights into the configured models/ directory.

Run this yourself after installing the Python dependencies:

    .venv\\Scripts\\python.exe scripts\\prefetch_docling_models.py

It first invokes ``docling-tools models download`` for the models this project
uses (layout, TableFormer, CodeFormula and RapidOCR), then parses a tiny
synthetic PDF to prove the weights load. No user documents are read and the
knowledge database is not touched.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODELS = ("layout", "tableformer", "code_formula", "rapidocr")


def _tool_path() -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    candidate = Path(sys.executable).with_name(f"docling-tools{suffix}")
    if candidate.is_file():
        return candidate
    return Path(f"docling-tools{suffix}")


def main() -> int:
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        try:
            import fitz  # noqa: F401
        except ImportError:
            print("[失败] 请先安装依赖：uv add pymupdf")
            return 1
    try:
        import docling  # noqa: F401
    except ImportError:
        print('[失败] 请先安装依赖：uv add "docling[rapidocr]"')
        return 1

    from easy_office.config.manager import ConfigManager

    manager = ConfigManager()
    paths = manager.ensure_directories()
    config = manager.get()
    tool = _tool_path()
    print(f"模型缓存目录：{paths.weights_dir}")
    print("开始下载模型（首次运行需要稳定网络，模型体积较大）...")
    command = [
        str(tool),
        "models",
        "download",
        *MODELS,
        "--output-dir",
        str(paths.weights_dir),
        # Use the cross-platform ONNX backend for Chinese + English; this
        # avoids downloading the unused torch OCR checkpoint.
        "--rapidocr-backend-lang",
        "onnxruntime:ch",
        "--rapidocr-model-size",
        "small",
    ]
    print("执行：", " ".join(command))
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        print(f"[失败] 模型下载命令退出码：{completed.returncode}")
        return completed.returncode

    print("下载完成，开始验证模型加载...")
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore

    from easy_office.parsing.docling_parser import DoclingParser

    with tempfile.TemporaryDirectory(prefix="easyoffice-prefetch-") as temporary:
        work = Path(temporary)
        digital = work / "digital.pdf"
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 100), "Easy Office model prefetch sample.")
        page.insert_text((72, 140), "Formula: E = m c^2")
        page.insert_text((72, 180), "Item | Amount")
        document.save(digital)
        document.close()

        with fitz.open(digital) as source:
            pixmap = source.load_page(0).get_pixmap(dpi=150)
            image_path = work / "scan.png"
            pixmap.save(image_path)
        scanned = work / "scanned.pdf"
        document = fitz.open()
        page = document.new_page(width=595, height=842)
        page.insert_image(page.rect, filename=str(image_path))
        document.save(scanned)
        document.close()

        parser = DoclingParser(
            config.local_parser,
            weights_dir=paths.weights_dir,
            timeout_seconds=3600,
        )
        print("验证数字页（布局/表格/公式模型）...")
        parser.parse(digital, workspace=work / "digital-work")
        print("验证扫描页（OCR 模型）...")
        parser.parse(scanned, workspace=work / "scanned-work")

    print("[完成] Docling 模型已缓存到：", paths.weights_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
