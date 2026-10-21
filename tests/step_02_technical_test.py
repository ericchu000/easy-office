from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path

import pytest

from easy_office.config.models import (
    EmbeddingConfig,
    LocalParserConfig,
    VLMConfig,
)
from easy_office.parsing.assets import AssetCache
from easy_office.parsing.base import ParserRegistry, UnsupportedParserBackend
from easy_office.parsing.chunker import OfficeChunker
from easy_office.parsing.images import ImageProcessor
from easy_office.parsing.models import (
    PageProbe,
    ParsedBlock,
    ParsedCell,
    ParsedDocument,
    ParsedFormula,
    ParsedPicture,
    ParsedTable,
)
from easy_office.parsing.pipeline import OfficeIngestError
from easy_office.parsing.summary import (
    build_office_summary,
    carrier_type,
    detect_language,
)
from easy_office.parsing.tables import analyze_table, render_table

ROOT = Path(__file__).resolve().parents[1]
REAL_DATA_DIR = ROOT / "data"
REAL_CONFIG_PATH = ROOT / "config.yaml"
REAL_MODELS_DIR = ROOT / "models"
REAL_SERVICES = os.environ.get("EASYOFFICE_REAL_SERVICES") == "1"
DOCLING_AVAILABLE = importlib.util.find_spec("docling") is not None
FITZ_AVAILABLE = importlib.util.find_spec("fitz") is not None
PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None


def _require_real_services():
    if not REAL_SERVICES:
        pytest.skip("set EASYOFFICE_REAL_SERVICES=1 to run real-service tests")
    if not REAL_CONFIG_PATH.is_file():
        pytest.skip(f"real configuration not found: {REAL_CONFIG_PATH}")


def _real_config():
    from easy_office.config.manager import ConfigManager

    return ConfigManager(config_path=REAL_CONFIG_PATH).get()


def _real_documents(extension: str) -> list[Path]:
    if not REAL_DATA_DIR.is_dir():
        return []
    return [
        path
        for path in sorted(REAL_DATA_DIR.glob(f"*{extension}"))
        if path.is_file() and not path.name.startswith("~$")
    ]


def _real_image() -> Path | None:
    assets = ROOT / "runtime" / "assets"
    if not assets.is_dir():
        return None
    return next(iter(sorted(assets.rglob("picture-*.png"))), None)


def simple_table(header=("名称", "金额"), rows=(("差旅", "100"),)) -> ParsedTable:
    cells = [
        ParsedCell(value, 0, 1, index, index + 1, is_header=True)
        for index, value in enumerate(header)
    ]
    for row_index, row in enumerate(rows, start=1):
        for col_index, value in enumerate(row):
            cells.append(
                ParsedCell(value, row_index, row_index + 1, col_index, col_index + 1)
            )
    return ParsedTable(
        cells=cells,
        num_rows=len(rows) + 1,
        num_cols=len(header),
        page_no=1,
        bbox=(10.0, 20.0, 200.0, 90.0),
    )


def merged_table() -> ParsedTable:
    cells = [
        ParsedCell("年度经营数据", 0, 1, 0, 3, is_header=True),
        ParsedCell("季度", 1, 2, 0, 1, is_header=True),
        ParsedCell("收入", 1, 2, 1, 2, is_header=True),
        ParsedCell("成本", 1, 2, 2, 3, is_header=True),
        ParsedCell("Q1", 2, 3, 0, 1),
        ParsedCell("100", 2, 3, 1, 2),
        ParsedCell("60", 2, 3, 2, 3),
        ParsedCell("合计", 3, 5, 0, 1),  # rowspan 2
        ParsedCell("160", 3, 4, 1, 3),  # colspan 2, covers row 4 too
    ]
    return ParsedTable(cells=cells, num_rows=5, num_cols=3, page_no=2,
                       bbox=(5.0, 5.0, 300.0, 120.0))


# ---------------------------------------------------------------- table rules
def test_simple_table_becomes_markdown():
    render = render_table(simple_table())
    assert render.format == "markdown"
    assert render.structured_text.splitlines()[0] == "| 名称 | 金额 |"
    assert "| --- | --- |" in render.structured_text
    assert "差旅" in render.plain_text
    assert "表头" in render.plain_text


def test_merged_cells_become_minimal_html():
    render = render_table(merged_table())
    assert render.format == "html"
    assert render.structured_text.startswith("<table>")
    assert "<thead>" in render.structured_text
    assert 'colspan="3"' in render.structured_text
    assert 'rowspan="2"' in render.structured_text
    assert 'colspan="2"' in render.structured_text
    # Only the documented minimal tag set is emitted.
    for tag in ("<div", "<br", "class=", "<span"):
        assert tag not in render.structured_text


def test_multiline_cell_forces_html():
    table = simple_table(header=("说明", "值"), rows=(("第一行\n第二行", "1"),))
    assert analyze_table(table).multiline_cells is True
    assert render_table(table).format == "html"


def test_list_marker_forces_html():
    table = simple_table(header=("说明", "值"), rows=(("• 项目一\n• 项目二", "1"),))
    assert render_table(table).format == "html"


def test_multi_level_header_forces_html():
    assert analyze_table(merged_table()).multi_level_header is True
    assert render_table(merged_table()).format == "html"


def test_uneven_row_widths_forces_html():
    cells = [
        ParsedCell("A", 0, 1, 0, 1, is_header=True),
        ParsedCell("B", 0, 1, 1, 2, is_header=True),
        ParsedCell("only", 1, 2, 0, 1),
    ]
    table = ParsedTable(cells=cells, num_rows=2, num_cols=2)
    assert analyze_table(table).uneven_row_widths is True
    assert render_table(table).format == "html"


def test_html_escapes_text():
    from easy_office.parsing.tables import render_html

    table = simple_table(header=("名称", "金额"), rows=(("<报表&数据>", "1"),))
    table.cells[2].text = "<报表&数据>"
    html = render_html(table)
    assert "&lt;报表&amp;数据&gt;" in html


# -------------------------------------------------------------------- summary
def test_language_detection():
    assert detect_language("本季度营业收入同比增长百分之十。") == "zh"
    assert detect_language("Quarterly revenue grew by ten percent.") == "en"
    assert detect_language("これは日本語の文書です。") == "ja"
    assert detect_language("한국어 문서입니다.") == "ko"
    assert detect_language("12345 !!!") == "unknown"


def test_carrier_type_digital_scanned_mixed():
    def doc(pages):
        return ParsedDocument(
            source_path="x.pdf",
            source_format="pdf",
            parser_backend="docling",
            blocks=[ParsedBlock(kind="text", text="hello")],
            pages=pages,
            page_semantics="physical",
            page_count=len(pages),
        )

    digital = [PageProbe(page_no=1, is_scanned=False)]
    scanned = [PageProbe(page_no=1, is_scanned=True)]
    mixed = [PageProbe(page_no=1, is_scanned=False), PageProbe(page_no=2, is_scanned=True)]
    assert carrier_type(doc(digital)) == "digital"
    assert carrier_type(doc(scanned)) == "scanned"
    assert carrier_type(doc(mixed)) == "mixed"


def test_office_summary_flow_and_flags():
    table = simple_table()
    formula = ParsedFormula(text="E=mc^2", page_no=1)
    picture = ParsedPicture(width=300, height=200, page_no=1, description="图表")
    document = ParsedDocument(
        source_path="a.docx",
        source_format="docx",
        parser_backend="docling",
        blocks=[
            ParsedBlock(kind="text", text="中文正文内容", page_no=1, order=0),
            ParsedBlock(kind="table", page_no=1, order=1, table=table),
            ParsedBlock(kind="formula", page_no=1, order=2, formula=formula),
            ParsedBlock(kind="picture", page_no=1, order=3, picture=picture),
        ],
        pages=[PageProbe(page_no=1)],
        page_semantics="flow",
        page_count=1,
    )
    summary = build_office_summary(document)
    assert summary["total_pages"] == 1
    assert summary["has_tables"] is True
    assert summary["has_formulas"] is True
    assert summary["has_charts_or_images"] is True
    assert summary["table_count"] == 1
    assert summary["source_format"] == "docx"
    assert summary["total_characters"] > 0


# -------------------------------------------------------------------- chunker
def _document(pages=1, semantic="physical"):
    table = simple_table()
    return ParsedDocument(
        source_path="doc.pdf",
        source_format="pdf",
        parser_backend="docling",
        blocks=[
            ParsedBlock(kind="heading", text="第一章 总则", level=1, page_no=1,
                        bbox=(10, 10, 100, 20), order=0),
            ParsedBlock(kind="text", text="这是第一段正文。", page_no=1,
                        bbox=(10, 25, 300, 50), order=1),
            ParsedBlock(kind="text", text="这是第二页正文。", page_no=2,
                        bbox=(10, 25, 300, 50), order=2),
            ParsedBlock(kind="table", page_no=2, bbox=(10, 60, 200, 100),
                        order=3, table=table),
            ParsedBlock(kind="formula", page_no=2, bbox=(10, 110, 100, 130),
                        order=4, formula=ParsedFormula(text="a^2+b^2=c^2", page_no=2)),
        ],
        pages=[PageProbe(page_no=i + 1, width=600, height=800) for i in range(pages)],
        page_semantics=semantic,
        page_count=pages,
    )


def test_chunks_have_section_page_and_bbox():
    chunks = OfficeChunker(LocalParserConfig()).build(_document(pages=2))
    text_chunks = [chunk for chunk in chunks if chunk.chunk_type == "text"]
    assert len(text_chunks) == 2
    assert text_chunks[0].plain_text.startswith("[章节: 第一章 总则 | 第 1 页]")
    assert text_chunks[0].page_start == 1
    assert text_chunks[0].bbox == (10.0, 25.0, 300.0, 50.0)
    assert text_chunks[1].page_start == 2
    assert text_chunks[1].plain_text.startswith("[章节: 第一章 总则 | 第 2 页]")
    assert [chunk.chunk_order for chunk in chunks] == list(range(len(chunks)))


def test_flow_page_prefix_is_omitted():
    chunks = OfficeChunker(LocalParserConfig()).build(_document(pages=1, semantic="flow"))
    assert chunks[0].plain_text.startswith("[章节: 第一章 总则]")


def test_table_and_formula_chunks():
    chunks = OfficeChunker(LocalParserConfig()).build(_document(pages=2))
    table = next(chunk for chunk in chunks if chunk.chunk_type == "table")
    assert table.page_start == 2
    assert table.metadata["table_format"] in {"markdown", "html"}
    assert "名称" in table.plain_text
    formula = next(chunk for chunk in chunks if chunk.chunk_type == "formula")
    assert formula.structured_text == "a^2+b^2=c^2"


def test_picture_chunk_binds_context_and_asset():
    picture = ParsedPicture(
        width=400,
        height=300,
        page_no=1,
        caption="图1 收入趋势",
        context_before="前文说明",
        context_after="后文结论",
        description="一张折线图，显示收入上涨。",
        description_source="vlm",
        visual_tags=["折线图", "收入"],
        asset_path="/tmp/pic.png",
        ocr_text="2024 2025",
    )
    document = ParsedDocument(
        source_path="a.pdf",
        source_format="pdf",
        parser_backend="docling",
        blocks=[
            ParsedBlock(kind="heading", text="经营分析", level=1, page_no=1, order=0),
            ParsedBlock(kind="picture", page_no=1, order=1, picture=picture),
        ],
        pages=[PageProbe(page_no=1, width=600, height=800)],
        page_semantics="physical",
        page_count=1,
    )
    chunks = OfficeChunker(LocalParserConfig()).build(document)
    image_chunk = next(chunk for chunk in chunks if chunk.chunk_type == "image")
    assert image_chunk.image_path == "/tmp/pic.png"
    assert image_chunk.page_start == 1
    assert "图注：图1 收入趋势" in image_chunk.plain_text
    assert "上下文：前文说明 后文结论" in image_chunk.plain_text
    assert "图片描述：一张折线图" in image_chunk.plain_text
    assert "图中文字：2024 2025" in image_chunk.plain_text
    assert image_chunk.metadata["visual_tags"] == ["折线图", "收入"]


# --------------------------------------------------------------- image rules
def _picture(size, bbox, page=1, order=0, payload=None):
    width, height = size
    if payload is None:
        payload = b"png-bytes"
    return ParsedPicture(
        width=width, height=height, page_no=page, bbox=bbox, order=order,
        image_bytes=payload,
    )


def _image_document(pictures):
    return ParsedDocument(
        source_path="a.pdf",
        source_format="pdf",
        parser_backend="docling",
        blocks=[
            ParsedBlock(kind="picture", page_no=p.page_no, order=i, picture=p)
            for i, p in enumerate(pictures)
        ],
        pages=[
            PageProbe(page_no=1, width=600, height=800),
            PageProbe(page_no=2, width=600, height=800),
        ],
        page_semantics="physical",
        page_count=2,
    )


@pytest.mark.skipif(not PIL_AVAILABLE, reason="Pillow is not installed")
def test_decorative_images_are_filtered(tmp_path):
    from PIL import Image

    def png(width, height):
        buffer = io.BytesIO()
        Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
        return buffer.getvalue()

    pictures = [
        _picture((300, 200), (10, 300, 310, 500), page=1, order=0, payload=png(300, 200)),
        _picture((30, 30), (10, 10, 40, 40), page=1, order=1, payload=png(30, 30)),
        _picture((700, 50), (10, 400, 710, 450), page=1, order=2, payload=png(700, 50)),
        _picture((200, 100), (10, 5, 210, 105), page=1, order=3, payload=png(200, 100)),
        _picture((200, 100), (10, 5, 210, 105), page=2, order=4, payload=png(200, 100)),
        _picture((600, 800), (0, 0, 600, 800), page=1, order=5, payload=png(600, 800)),
    ]
    stats = ImageProcessor(
        LocalParserConfig(image_min_side_px=50), VLMConfig(), AssetCache(tmp_path)
    ).process(_image_document(pictures), "uid_filter_1")
    assert [picture.filtered_reason for picture in pictures] == [
        None,
        "small_side",
        "extreme_aspect",
        "repeated_header_footer",
        "repeated_header_footer",
        "page_image",
    ]
    assert stats.total == 6
    assert stats.kept == 1
    assert stats.filtered == 5
    assert pictures[0].asset_path is not None


def test_repeated_non_header_image_is_kept(tmp_path):
    pictures = [
        _picture((200, 100), (100, 300, 300, 400), page=1, order=0),
        _picture((200, 100), (100, 300, 300, 400), page=2, order=1),
    ]
    stats = ImageProcessor(LocalParserConfig(), VLMConfig(), AssetCache(tmp_path)).process(
        _image_document(pictures), "uid_filter_2"
    )
    assert stats.kept == 2


def test_image_uses_ocr_fallback_when_vlm_not_configured(tmp_path):
    pictures = [_picture((400, 300), (10, 100, 410, 400), page=1)]
    stats = ImageProcessor(LocalParserConfig(), VLMConfig(), AssetCache(tmp_path)).process(
        _image_document(pictures), "uid_enrich_2"
    )
    picture = pictures[0]
    assert stats.vlm_described == 0
    assert picture.description_source == "fallback"
    assert "400×300" in picture.description


# ------------------------------------------------------------------ PDF probe
@pytest.mark.skipif(not FITZ_AVAILABLE, reason="PyMuPDF is not installed")
def test_pdf_probe_and_split_real_documents(tmp_path):
    from easy_office.parsing.pdf_probe import probe_pdf, split_pdf

    digital = None
    scanned = None
    for path in _real_documents(".pdf"):
        probes = probe_pdf(path)
        if digital is None and any(not page.is_scanned for page in probes):
            digital = (path, probes)
        if scanned is None and any(page.is_scanned for page in probes):
            scanned = (path, probes)
    if digital is None or scanned is None:
        pytest.skip("real digital and scanned PDFs are required under data/")

    digital_path, digital_probes = digital
    scanned_path, scanned_probes = scanned
    assert any(not page.is_scanned for page in digital_probes)
    assert any(page.is_scanned for page in scanned_probes)
    assert sum(page.char_count for page in digital_probes) > 0
    assert max(page.image_coverage for page in scanned_probes) > 0.1

    subset = split_pdf(scanned_path, [1], tmp_path / "subset.pdf")
    subset_probes = probe_pdf(subset)
    assert len(subset_probes) == 1
    assert subset_probes[0].is_scanned is True
    assert digital_path.is_file()


# --------------------------------------------------------- real model services
def test_embedding_and_vision_unconfigured_return_none():
    from easy_office.models.services import build_embedding_client, build_vision_client

    assert build_embedding_client(EmbeddingConfig()) is None
    assert build_vision_client(VLMConfig()) is None


def test_real_embedding_service_dimension():
    _require_real_services()
    from easy_office.models.services import build_embedding_client

    config = _real_config().embedding
    client = build_embedding_client(config)
    if client is None:
        pytest.skip("real Embedding service is not configured")
    vector = client.embed_query("风电功率预测")
    assert len(vector) == config.embedding_dim


def test_real_embedding_batch_limit_split():
    _require_real_services()
    from easy_office.models.services import build_embedding_client

    config = _real_config().embedding
    client = build_embedding_client(config)
    if client is None:
        pytest.skip("real Embedding service is not configured")
    texts = [f"电力系统功率预测测试文本 {index}" for index in range(25)]
    vectors = client.embed_texts(texts)
    assert len(vectors) == len(texts)
    assert all(len(vector) == config.embedding_dim for vector in vectors)


def test_real_vlm_describes_real_cropped_image():
    _require_real_services()
    from easy_office.models.services import build_vision_client

    image = _real_image()
    if image is None:
        pytest.skip("no real cropped image under runtime/assets")
    config = _real_config().vlm
    client = build_vision_client(config)
    if client is None:
        pytest.skip("real VLM service is not configured")
    description = client.describe_image(image.read_bytes())
    assert description.detailed_caption or description.scene_category


# --------------------------------------------------------------- pipeline E2E
def test_registry_rejects_unknown_backend():
    registry = ParserRegistry()
    with pytest.raises(UnsupportedParserBackend):
        registry.create("mineru", LocalParserConfig())


def test_pipeline_rejects_non_office_extension(core, tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    record = core.register_inplace(source)
    with pytest.raises(OfficeIngestError):
        core.office.ingest(record.file_uid)


# -------------------------------------------------------------- asset config
def test_asset_cache_directory_is_configured(core):
    paths = core.config.paths()
    assert paths.asset_cache_dir.name == "assets"
    assert paths.asset_cache_dir.is_dir()
    assert core.assets.root == paths.asset_cache_dir


def test_formula_heuristic_flags_equation_text():
    document = ParsedDocument(
        source_path="a.docx",
        source_format="docx",
        parser_backend="docling",
        blocks=[
            ParsedBlock(
                kind="text",
                text="预测误差定义为 MAPE = sum(|y - y_hat|) / sum(y)。",
                page_no=1,
                order=0,
            )
        ],
        pages=[PageProbe(page_no=1)],
        page_semantics="flow",
        page_count=1,
    )
    summary = build_office_summary(document)
    assert summary["has_formulas"] is True
    assert summary["formula_detection"] == "heuristic"


def test_native_page_formula_hint_sets_flag():
    document = ParsedDocument(
        source_path="a.pdf",
        source_format="pdf",
        parser_backend="docling",
        blocks=[ParsedBlock(kind="text", text="Only prose here.", page_no=1, order=0)],
        pages=[PageProbe(page_no=1, formula_hint=True)],
        page_semantics="physical",
        page_count=1,
    )
    summary = build_office_summary(document)
    assert summary["has_formulas"] is True
    assert summary["formula_detection"] == "native_text"
    assert summary["formula_hint_pages"] == 1


def test_libreoffice_missing_path_is_reported():
    from easy_office.parsing.libreoffice import (
        LibreOfficeNotFound,
        discover_libreoffice,
    )

    with pytest.raises(LibreOfficeNotFound):
        discover_libreoffice(str(Path("C:/definitely/missing/soffice.exe")))


def test_merged_pdf_parts_keep_page_order():
    from easy_office.parsing.docling_parser import DoclingParser

    def part(page, text):
        return ParsedDocument(
            source_path="x.pdf",
            source_format="pdf",
            parser_backend="docling",
            blocks=[ParsedBlock(kind="text", text=text, page_no=page, order=0)],
            page_semantics="physical",
        )

    merged = DoclingParser(LocalParserConfig())._merge_parts(
        [part(1, "scanned page 1"), part(3, "digital page 3")],
        Path("x.pdf"),
        "pdf",
    )
    assert [block.page_no for block in merged.blocks] == [1, 3]
    assert [block.order for block in merged.blocks] == [0, 1]


# ------------------------------------------------------------- real Docling
@pytest.mark.skipif(not DOCLING_AVAILABLE, reason="Docling is not installed")
def test_docling_parses_real_docx_and_pptx():
    from easy_office.parsing.docling_parser import DoclingParser

    docx_files = _real_documents(".docx")
    pptx_files = _real_documents(".pptx")
    if not docx_files or not pptx_files:
        pytest.skip("real DOCX and PPTX documents are required under data/")
    if not REAL_MODELS_DIR.is_dir() or not any(REAL_MODELS_DIR.iterdir()):
        pytest.skip("Docling model weights are required under models/")

    parser = DoclingParser(LocalParserConfig(), weights_dir=REAL_MODELS_DIR)
    parsed = parser.parse(docx_files[0])
    assert parsed.page_semantics == "flow"
    assert parsed.page_count == 1
    assert any(block.kind in {"heading", "text"} for block in parsed.blocks)
    assert parsed.tables
    assert parsed.pictures and parsed.pictures[0].image_bytes

    parsed = parser.parse(pptx_files[0])
    assert parsed.page_semantics == "slide"
    assert parsed.page_count > 0
    assert any(block.kind in {"heading", "text"} for block in parsed.blocks)
    assert parsed.pictures


# ------------------------------------------------------ real documents (opt-in)
STEP2_DOCS = os.environ.get("EASYOFFICE_STEP2_DOCS", "")
STEP2_MODELS = os.environ.get("EASYOFFICE_DOCLING_ARTIFACTS", "")


@pytest.mark.skipif(not DOCLING_AVAILABLE, reason="Docling is not installed")
@pytest.mark.skipif(not STEP2_DOCS, reason="EASYOFFICE_STEP2_DOCS is not set")
def test_real_documents_ingest(tmp_path):
    from easy_office.core import EasyOfficeCore

    docs_dir = Path(STEP2_DOCS)
    if not docs_dir.is_dir():
        pytest.skip(f"EASYOFFICE_STEP2_DOCS is not a directory: {docs_dir}")
    weights_dir = Path(STEP2_MODELS) if STEP2_MODELS else ROOT / "models"
    if not weights_dir.is_dir() or not any(weights_dir.iterdir()):
        pytest.skip(
            "no Docling model weights found; run scripts/prefetch_docling_models.py "
            "or set EASYOFFICE_DOCLING_ARTIFACTS"
        )
    runtime_dirs = {"assets", "index", "mirror", "managed", "models"}
    files = []
    for path in sorted(docs_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("~$"):
            continue
        if path.suffix.lower().lstrip(".") not in {"pdf", "docx", "pptx", "doc", "ppt"}:
            continue
        relative = path.relative_to(docs_dir)
        if relative.parts and relative.parts[0] in runtime_dirs:
            continue
        files.append(path)
    if not files:
        pytest.skip(f"no office documents found under {docs_dir}")
    core = EasyOfficeCore.bootstrap(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / "runtime",
        config_patch={
            "embedding": {"embedding_dim": 1024},
            # This invariant test focuses on routing/OCR/summary; the real
            # formula model is exercised separately (and is very slow on CPU).
            "local_parser": {
                "weights_dir": str(weights_dir),
                "formula_enrichment": False,
            },
        },
    )
    try:
        pipeline = core.office
        for path in files:
            result = pipeline.ingest_path(path)
            assert result.chunk_count > 0, path
            assert result.page_count > 0, path
            stored = core.metadata.get_file(result.file_uid)
            assert stored is not None
            assert stored.modality_summary_kind == "office"
            summary = stored.modality_summary
            for key in (
                "total_pages",
                "total_characters",
                "primary_language",
                "carrier_type",
                "has_tables",
                "has_formulas",
                "has_charts_or_images",
            ):
                assert key in summary, (path, key)
            assert core.mirror.exists(result.file_uid), path
    finally:
        core.close()
