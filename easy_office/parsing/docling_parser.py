"""Docling driver: the default layout-aware office document parser.

Design notes
------------
* Docling is imported lazily. The rest of the offline core (config, metadata,
  indexing) must keep working on machines where the heavy parser stack is not
  installed yet.
* PDFs are probed with PyMuPDF first. All-digital and all-scanned documents go
  through one conversion; mixed documents are split into two page subsets with
  the correct OCR switches and merged back onto the original page numbers.
* DOCX uses the user-approved flow-page semantics: Docling's native Word
  backend has no physical pagination, so every block is reported on page 1.
"""

from __future__ import annotations

import gc
import io
import os
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from easy_office.config.models import LocalParserConfig
from easy_office.parsing.libreoffice import LibreOfficeConverter
from easy_office.parsing.models import (
    PageProbe,
    ParsedBlock,
    ParsedCell,
    ParsedDocument,
    ParsedFormula,
    ParsedPicture,
    ParsedTable,
)
from easy_office.parsing.pdf_probe import (
    digital_and_scanned_pages,
    page_count as pdf_page_count,
    probe_pdf,
    split_pdf,
)

_PHYSICAL_FORMATS = {"pdf"}
_FLOW_FORMATS = {"docx"}
_SLIDE_FORMATS = {"pptx"}
SUPPORTED_NATIVE_FORMATS = _PHYSICAL_FORMATS | _FLOW_FORMATS | _SLIDE_FORMATS
_SKIPPED_LABELS = {"page_header", "page_footer"}
_CAPTION_PATTERN = re.compile(
    r"((?:图|Figure|Fig\.?|表|Table)\s*\d+(?:[.\-]\d+)?\s*[^\n]{0,160})\s*$",
    re.IGNORECASE,
)


def _guess_caption(text: str) -> str:
    """Recover a caption Docling did not link for DOCX/PPTX pictures."""
    if not text:
        return ""
    match = _CAPTION_PATTERN.search(text[-400:])
    return match.group(1).strip() if match else ""


class DoclingNotInstalled(RuntimeError):
    """Raised when the Docling parser stack is unavailable."""


class DocumentParseError(RuntimeError):
    """Raised when a document cannot be parsed."""


def _import_docling():
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DoclingNotInstalled(
            "Docling is not installed. Install it with: "
            'uv add "docling[rapidocr]"'
        ) from exc
    try:
        from docling.datamodel.pipeline_options import PdfPipelineOptions
    except ImportError:  # pragma: no cover - very old layout
        PdfPipelineOptions = None  # type: ignore
    try:
        from docling.datamodel.pipeline_options import RapidOcrOptions
    except ImportError:  # pragma: no cover - optional OCR backend
        RapidOcrOptions = None  # type: ignore
    return {
        "InputFormat": InputFormat,
        "DocumentConverter": DocumentConverter,
        "PdfFormatOption": PdfFormatOption,
        "PdfPipelineOptions": PdfPipelineOptions,
        "RapidOcrOptions": RapidOcrOptions,
    }


def _label(item: Any) -> str:
    value = getattr(item, "label", "")
    text = getattr(value, "value", value)
    return str(text or "").lower()


def _class_name(item: Any) -> str:
    return type(item).__name__.lower()


def _item_text(item: Any) -> str:
    text = getattr(item, "text", "")
    if text is None:
        return ""
    return str(text)


def _prov_of(item: Any) -> Any | None:
    try:
        provenance = getattr(item, "prov", None)
        if provenance:
            return list(provenance)[0]
    except (TypeError, IndexError):
        pass
    return None


def _page_and_bbox(
    item: Any, page_sizes: dict[int, tuple[float, float]]
) -> tuple[int | None, tuple[float, float, float, float] | None]:
    provenance = _prov_of(item)
    if provenance is None:
        return None, None
    page_no = getattr(provenance, "page_no", None)
    page_no = int(page_no) if page_no is not None else None
    bbox = getattr(provenance, "bbox", None)
    if bbox is None:
        return page_no, None
    try:
        left = float(getattr(bbox, "l", getattr(bbox, "x0", 0.0)))
        top = float(getattr(bbox, "t", getattr(bbox, "y0", 0.0)))
        right = float(getattr(bbox, "r", getattr(bbox, "x1", 0.0)))
        bottom = float(getattr(bbox, "b", getattr(bbox, "y1", 0.0)))
    except (TypeError, ValueError):
        return page_no, None
    height = page_sizes.get(page_no or 0, (0.0, 0.0))[1]
    if height > 0:
        # Docling reports PDF coordinates with a bottom-left origin. Convert
        # to the top-left origin used by our chunk contract.
        y0 = height - top
        y1 = height - bottom
        return page_no, (left, min(y0, y1), right, max(y0, y1))
    return page_no, (left, min(top, bottom), right, max(top, bottom))


def _resolve_refs(document: Any, refs: Iterable[Any], item_map: dict[str, Any]) -> list[Any]:
    resolved: list[Any] = []
    for ref in refs:
        cref = getattr(ref, "cref", None) or getattr(ref, "self_ref", None)
        if cref is None:
            continue
        target = item_map.get(str(cref))
        if target is None:
            resolver = getattr(ref, "resolve", None)
            if callable(resolver):
                try:
                    target = resolver(document)
                except TypeError:
                    try:
                        target = resolver(doc=document)
                    except Exception:
                        target = None
                except Exception:
                    target = None
        if target is None:
            document_resolver = getattr(document, "resolve", None)
            if callable(document_resolver):
                try:
                    target = document_resolver(ref)
                except Exception:
                    target = None
        if target is not None:
            resolved.append(target)
    return resolved


def _caption_texts(document: Any, item: Any, item_map: dict[str, Any]) -> list[str]:
    caption_getter = getattr(item, "caption_text", None)
    if callable(caption_getter):
        for args, kwargs in (((), {}), ((document,), {}), ((), {"doc": document})):
            try:
                text = str(caption_getter(*args, **kwargs) or "").strip()
            except Exception:
                continue
            if text:
                return [text]
            break
    refs: list[Any] = []
    for name in ("captions", "caption"):
        value = getattr(item, name, None)
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            refs.extend(value)
        else:
            refs.append(value)
    texts: list[str] = []
    for target in _resolve_refs(document, refs, item_map):
        text = _item_text(target).strip()
        if text:
            texts.append(text)
    return texts


def _caption_target_ids(item: Any) -> set[str]:
    ids: set[str] = set()
    for name in ("captions", "caption"):
        value = getattr(item, name, None)
        if not value:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for ref in values:
            cref = getattr(ref, "cref", None) or getattr(ref, "self_ref", None)
            if cref is not None:
                ids.add(str(cref))
    return ids


def _picture_image(item: Any, document: Any) -> tuple[bytes | None, int, int]:
    image: Any = None
    getter = getattr(item, "get_image", None)
    if callable(getter):
        try:
            image = getter(document)
        except Exception:
            image = None
    if image is None:
        image = getattr(item, "image", None)
    pil = None
    if image is not None:
        pil = getattr(image, "pil_image", None) or getattr(image, "image", None)
        if pil is None and hasattr(image, "convert"):
            pil = image
        if pil is None and hasattr(image, "data"):
            data = getattr(image, "data")
            if isinstance(data, (bytes, bytearray)):
                try:
                    from PIL import Image  # type: ignore

                    pil = Image.open(io.BytesIO(bytes(data)))
                except Exception:
                    pil = None
    if pil is None:
        return None, 0, 0
    try:
        from PIL import Image  # type: ignore

        if not hasattr(pil, "save"):
            return None, 0, 0
        width, height = int(pil.width), int(pil.height)
        buffer = io.BytesIO()
        # Copy so palette/alpha modes serialize predictably.
        pil.convert("RGB").save(buffer, format="PNG")
        return buffer.getvalue(), width, height
    except Exception:
        return None, 0, 0


class DoclingParser:
    """Default parser backend registered as ``docling``."""

    backend_name = "docling"
    supported_formats = SUPPORTED_NATIVE_FORMATS

    def __init__(
        self,
        config: LocalParserConfig,
        *,
        weights_dir: str | Path | None = None,
        timeout_seconds: float = 180.0,
    ) -> None:
        self._config = config
        self._weights_dir = Path(weights_dir) if weights_dir else None
        self._timeout = float(timeout_seconds)
        self._converter_cache: dict[tuple[bool, bool], Any] = {}
        self._module = None

    # ----------------------------------------------------------------- public
    def close(self) -> None:
        """Release converters and model references held by this parser."""
        self._converter_cache.clear()
        self._module = None
        gc.collect()

    def parse(
        self,
        path: str | Path,
        *,
        workspace: str | Path | None = None,
    ) -> ParsedDocument:
        source = Path(path)
        suffix = source.suffix.lower().lstrip(".")
        if suffix not in self.supported_formats:
            raise DocumentParseError(
                f"DoclingParser does not handle .{suffix}; expected one of "
                f"{sorted(self.supported_formats)} (legacy formats are converted first)"
            )
        if suffix == "pdf":
            return self._parse_pdf(source, workspace=workspace)
        document = self._convert(source, ocr=None)
        if suffix == "docx":
            return self._extract(
                document, source, source_format="docx", page_semantics="flow"
            )
        return self._extract(
            document, source, source_format="pptx", page_semantics="slide"
        )

    # ------------------------------------------------------------- conversion
    def _modules(self) -> dict:
        if self._module is None:
            if self._weights_dir is not None:
                try:
                    populated = any(self._weights_dir.iterdir())
                except OSError:
                    populated = False
                if populated:
                    os.environ.setdefault(
                        "DOCLING_ARTIFACTS_PATH", str(self._weights_dir)
                    )
                elif os.environ.get("DOCLING_ARTIFACTS_PATH") == str(self._weights_dir):
                    # Never leave the process pointing at an empty artifacts
                    # directory: Docling rejects that configuration even for
                    # formats (DOCX/PPTX) that need no pipeline models.
                    os.environ.pop("DOCLING_ARTIFACTS_PATH", None)
            self._module = _import_docling()
        return self._module

    def _pipeline_options(self, *, ocr: bool, force_full_page_ocr: bool):
        modules = self._modules()
        options_class = modules["PdfPipelineOptions"]
        if options_class is None:  # pragma: no cover - defensive
            return None
        options = options_class()
        self._set(options, "do_ocr", bool(ocr))
        self._set(options, "force_full_page_ocr", bool(force_full_page_ocr))
        self._set(options, "do_table_structure", True)
        # Digital pages already expose native text/vector content, so they keep
        # the fast path. Formula enrichment (a CPU-heavy VLM) runs only on the
        # scanned-page route, which is where the plan requires LaTeX recovery.
        self._set(
            options,
            "do_formula_enrichment",
            bool(self._config.formula_enrichment) and bool(force_full_page_ocr),
        )
        self._set(options, "generate_picture_images", True)
        self._set(options, "generate_page_images", False)
        # Scanned pages are rendered at the configured DPI; digital pages use
        # the cheaper 144 DPI equivalent.
        scale = (
            max(1.0, float(self._config.scanned_page_dpi) / 72.0)
            if force_full_page_ocr
            else 2.0
        )
        self._set(options, "images_scale", scale)
        accelerator = getattr(options, "accelerator_options", None)
        if accelerator is not None:
            self._set(accelerator, "device", self._config.device)
        if ocr:
            ocr_class = modules.get("RapidOcrOptions")
            if ocr_class is not None:
                try:
                    ocr_options = ocr_class()
                    self._set(ocr_options, "lang", self._rapidocr_languages())
                    # In Docling 2.x this flag lives on the OCR options object.
                    self._set(ocr_options, "force_full_page_ocr", bool(force_full_page_ocr))
                    self._set(options, "ocr_options", ocr_options)
                except Exception:
                    pass
        return options

    def _rapidocr_languages(self) -> list[str]:
        mapping = {
            "ch_sim": "ch",
            "zh": "ch",
            "en": "en",
            "ch_tra": "chinese_cht",
            "japan": "japan",
            "ko": "korean",
        }
        result: list[str] = []
        for item in self._config.ocr_languages:
            mapped = mapping.get(item.lower(), item)
            if mapped not in result:
                result.append(mapped)
        return result or ["ch"]

    @staticmethod
    def _set(target: Any, name: str, value: Any) -> None:
        if target is None or not hasattr(target, name):
            return
        try:
            setattr(target, name, value)
        except Exception:
            pass

    def _converter(self, *, ocr: bool, force_full_page_ocr: bool):
        key = (bool(ocr), bool(force_full_page_ocr))
        cached = self._converter_cache.get(key)
        if cached is not None:
            return cached
        modules = self._modules()
        options = self._pipeline_options(ocr=ocr, force_full_page_ocr=force_full_page_ocr)
        if options is not None:
            converter = modules["DocumentConverter"](
                format_options={
                    modules["InputFormat"].PDF: modules["PdfFormatOption"](
                        pipeline_options=options
                    )
                }
            )
        else:  # pragma: no cover - defensive against old Docling
            converter = modules["DocumentConverter"]()
        self._converter_cache[key] = converter
        return converter

    def _convert(self, source: Path, *, ocr: bool | None):
        modules = self._modules()
        if ocr is None:
            converter = modules["DocumentConverter"]()
        else:
            converter = self._converter(ocr=ocr, force_full_page_ocr=bool(ocr))
        try:
            result = converter.convert(str(source))
        except Exception as exc:
            raise DocumentParseError(f"Docling failed to parse {source.name}: {exc}") from exc
        status = getattr(result, "status", None)
        if status is not None and "SUCCESS" not in str(status).upper():
            raise DocumentParseError(
                f"Docling could not parse {source.name}: status={status}"
            )
        document = getattr(result, "document", None)
        if document is None:
            raise DocumentParseError(f"Docling returned no document for {source.name}")
        return document

    # ------------------------------------------------------------------- PDF
    def _parse_pdf(
        self, source: Path, *, workspace: str | Path | None = None
    ) -> ParsedDocument:
        probes = probe_pdf(
            source,
            min_chars=self._config.digital_text_min_chars,
            scan_image_area_ratio=self._config.scan_image_area_ratio,
        )
        digital, scanned = digital_and_scanned_pages(probes)
        if not scanned:
            document = self._convert(source, ocr=False)
            parsed = self._extract(
                document, source, source_format="pdf", page_semantics="physical"
            )
            parsed.pages = probes
            parsed.page_count = len(probes)
            return parsed
        if not digital:
            document = self._convert(source, ocr=True)
            parsed = self._extract(
                document, source, source_format="pdf", page_semantics="physical"
            )
            parsed.pages = probes
            parsed.page_count = len(probes)
            return parsed

        work_dir = Path(workspace) if workspace else source.parent
        work_dir.mkdir(parents=True, exist_ok=True)
        parsed_parts: list[ParsedDocument] = []
        for label, pages, ocr in (
            ("digital", digital, False),
            ("scanned", scanned, True),
        ):
            subset_path = work_dir / f"{source.stem}.pages-{label}.pdf"
            split_pdf(source, pages, subset_path)
            document = self._convert(subset_path, ocr=ocr)
            mapping = {index + 1: page_no for index, page_no in enumerate(pages)}
            parsed_parts.append(
                self._extract(
                    document,
                    source,
                    source_format="pdf",
                    page_semantics="physical",
                    page_mapping=mapping,
                )
            )
        merged = self._merge_parts(parsed_parts, source, "pdf")
        merged.pages = probes
        merged.page_count = len(probes)
        merged.warnings.append(
            f"mixed pdf routed by page: digital={digital}, scanned={scanned}"
        )
        return merged

    # -------------------------------------------------------------- extraction
    def _page_sizes(self, document: Any) -> dict[int, tuple[float, float]]:
        sizes: dict[int, tuple[float, float]] = {}
        pages = getattr(document, "pages", None) or {}
        try:
            items = pages.items()
        except AttributeError:
            items = []
        for page_no, page in items:
            size = getattr(page, "size", None)
            width = float(getattr(size, "width", 0.0) or 0.0)
            height = float(getattr(size, "height", 0.0) or 0.0)
            sizes[int(page_no)] = (width, height)
        return sizes

    def _extract(
        self,
        document: Any,
        source: Path,
        *,
        source_format: str,
        page_semantics: str,
        page_mapping: dict[int, int] | None = None,
    ) -> ParsedDocument:
        page_sizes = self._page_sizes(document)
        try:
            entries = [
                item
                for item, _level in document.iterate_items(
                    with_groups=False, traverse_pictures=True
                )
            ]
        except TypeError:  # pragma: no cover - older Docling signature
            try:
                entries = [item for item, _level in document.iterate_items()]
            except Exception as exc:
                raise DocumentParseError(
                    f"Docling document iteration failed: {exc}"
                ) from exc
        except Exception as exc:
            raise DocumentParseError(f"Docling document iteration failed: {exc}") from exc
        item_map: dict[str, Any] = {}
        for item in entries:
            ref = getattr(item, "self_ref", None)
            if ref is not None:
                item_map[str(ref)] = item

        consumed_captions: set[str] = set()
        for item in entries:
            consumed_captions.update(_caption_target_ids(item))

        # Picture context needs neighbours in reading order, so build a small
        # index of text-bearing positions first.
        text_positions = [
            index
            for index, item in enumerate(entries)
            if _label(item) not in _SKIPPED_LABELS and _item_text(item).strip()
        ]

        blocks: list[ParsedBlock] = []
        tables: list[ParsedTable] = []
        pictures: list[ParsedPicture] = []
        formulas: list[ParsedFormula] = []
        order = 0
        for index, item in enumerate(entries):
            label = _label(item)
            class_name = _class_name(item)
            if label in _SKIPPED_LABELS:
                continue
            if str(getattr(item, "self_ref", "")) in consumed_captions:
                # Caption text is attached to its table/picture block instead
                # of becoming a duplicate text chunk.
                continue
            if isinstance(getattr(item, "captions", None), (list, tuple)):
                consumed_captions.update(_caption_target_ids(item))
            page_no, bbox = _page_and_bbox(item, page_sizes)
            if page_mapping is not None and page_no is not None:
                page_no = page_mapping.get(page_no, page_no)
            if page_semantics == "flow":
                page_no = 1

            if "table" in class_name or label == "table":
                table = self._extract_table(item, document, page_no, bbox, order, item_map)
                if table is None or table.is_empty:
                    continue
                tables.append(table)
                blocks.append(
                    ParsedBlock(
                        kind="table",
                        page_no=page_no,
                        bbox=bbox,
                        order=order,
                        table=table,
                    )
                )
                order += 1
                continue

            if "picture" in class_name or label == "picture":
                picture = self._extract_picture(item, document, page_no, bbox, order, item_map)
                if picture is None:
                    continue
                picture.context_before = self._neighbour_text(
                    entries, index, text_positions, direction=-1
                )
                picture.context_after = self._neighbour_text(
                    entries, index, text_positions, direction=1
                )
                if not picture.caption:
                    picture.caption = _guess_caption(picture.context_before)
                pictures.append(picture)
                blocks.append(
                    ParsedBlock(
                        kind="picture",
                        page_no=page_no,
                        bbox=bbox,
                        order=order,
                        picture=picture,
                    )
                )
                order += 1
                continue

            if "formula" in class_name or label == "formula":
                formula = self._extract_formula(item, page_no, bbox, order)
                if formula is None or not formula.display_text:
                    continue
                formulas.append(formula)
                blocks.append(
                    ParsedBlock(
                        kind="formula",
                        page_no=page_no,
                        bbox=bbox,
                        order=order,
                        formula=formula,
                    )
                )
                order += 1
                continue

            text = _item_text(item).strip()
            if not text:
                continue
            if "sectionheader" in class_name or label == "section_header":
                level = int(getattr(item, "level", 1) or 1)
                kind = "heading"
            elif "list" in class_name or label in {"list_item", "list"}:
                kind = "list"
                level = 0
                marker = str(getattr(item, "marker", "") or "").strip()
                if marker and not text.lstrip().startswith(marker):
                    text = f"{marker} {text}"
            elif "code" in class_name or label == "code":
                kind = "code"
                level = 0
            else:
                kind = "text"
                level = 0
            blocks.append(
                ParsedBlock(
                    kind=kind,
                    text=text,
                    level=level,
                    page_no=page_no,
                    bbox=bbox,
                    order=order,
                )
            )
            order += 1

        if page_semantics == "flow":
            page_count = 1
            pages = [PageProbe(page_no=1, char_count=0, is_scanned=False)]
        else:
            page_count = len(page_sizes) or self._max_page(blocks)
            pages = [
                PageProbe(
                    page_no=page_no,
                    width=size[0],
                    height=size[1],
                    is_scanned=False,
                )
                for page_no, size in sorted(page_sizes.items())
            ]
            if not pages:
                observed = sorted({block.page_no for block in blocks if block.page_no})
                pages = [PageProbe(page_no=number) for number in observed]
        if page_semantics == "slide":
            self._promote_slide_titles(blocks)
        return ParsedDocument(
            source_path=str(source),
            source_format=source_format,
            parser_backend=self.backend_name,
            blocks=blocks,
            pages=pages,
            page_semantics=page_semantics,
            page_count=page_count,
        )

    @staticmethod
    def _max_page(blocks: Sequence[ParsedBlock]) -> int:
        values = [block.page_no for block in blocks if block.page_no is not None]
        return max(values) if values else 0

    @staticmethod
    def _promote_slide_titles(blocks: Sequence[ParsedBlock]) -> None:
        """Docling's PPTX backend reports slide titles as plain text.

        The first text block is often a section number ("一", "1"); prefer the
        first substantive text on the slide so breadcrumbs read naturally.
        """
        by_page: dict[int, list[ParsedBlock]] = {}
        for block in blocks:
            if block.page_no is not None:
                by_page.setdefault(block.page_no, []).append(block)
        for page_blocks in by_page.values():
            candidates = [
                block
                for block in page_blocks
                if block.kind == "text" and block.text.strip()
            ]
            if not candidates:
                continue
            chosen = candidates[0]
            for candidate in candidates:
                text = candidate.text.strip()
                if len(text) >= 4 and not text.isdigit():
                    chosen = candidate
                    break
            chosen.kind = "heading"
            chosen.level = 1

    @staticmethod
    def _neighbour_text(
        entries: Sequence[Any],
        index: int,
        text_positions: Sequence[int],
        *,
        direction: int,
    ) -> str:
        candidates = (
            [pos for pos in text_positions if pos < index][::-1]
            if direction < 0
            else [pos for pos in text_positions if pos > index]
        )
        for position in candidates[:2]:
            candidate = entries[position]
            label = _label(candidate)
            if label == "section_header" or "sectionheader" in _class_name(candidate):
                continue
            text = _item_text(candidate).strip()
            if text:
                return text[:500]
        return ""

    def _extract_table(
        self,
        item: Any,
        document: Any,
        page_no: int | None,
        bbox,
        order: int,
        item_map: dict[str, Any],
    ) -> ParsedTable | None:
        data = getattr(item, "data", None)
        if data is None:
            return None
        num_rows = int(getattr(data, "num_rows", 0) or 0)
        num_cols = int(getattr(data, "num_cols", 0) or 0)
        raw_cells = list(getattr(data, "table_cells", None) or [])
        if not raw_cells:
            grid = getattr(data, "grid", None) or []
            seen: set[int] = set()
            for row in grid:
                for cell in row:
                    if cell is None or id(cell) in seen:
                        continue
                    seen.add(id(cell))
                    raw_cells.append(cell)
        cells: list[ParsedCell] = []
        for cell in raw_cells:
            text = _item_text(cell).strip()
            row_start = int(getattr(cell, "start_row_offset_idx", 0) or 0)
            col_start = int(getattr(cell, "start_col_offset_idx", 0) or 0)
            row_span = int(getattr(cell, "row_span", 0) or 0)
            col_span = int(getattr(cell, "col_span", 0) or 0)
            if row_span <= 0:
                row_end = int(getattr(cell, "end_row_offset_idx", row_start) or row_start)
                row_span = max(1, row_end - row_start + 1)
            if col_span <= 0:
                col_end = int(getattr(cell, "end_col_offset_idx", col_start) or col_start)
                col_span = max(1, col_end - col_start + 1)
            cells.append(
                ParsedCell(
                    text=text,
                    row_start=row_start,
                    row_end=row_start + row_span,
                    col_start=col_start,
                    col_end=col_start + col_span,
                    is_header=bool(getattr(cell, "column_header", False)),
                    is_row_header=bool(getattr(cell, "row_header", False)),
                )
            )
            num_rows = max(num_rows, row_start + row_span)
            num_cols = max(num_cols, col_start + col_span)
        if not cells or num_rows <= 0 or num_cols <= 0:
            return None
        caption = "\n".join(
            _caption_texts(document, item, item_map)
        ).strip()
        return ParsedTable(
            cells=cells,
            num_rows=num_rows,
            num_cols=num_cols,
            page_no=page_no,
            bbox=bbox,
            caption=caption,
            order=order,
        )

    def _extract_picture(
        self,
        item: Any,
        document: Any,
        page_no: int | None,
        bbox,
        order: int,
        item_map: dict[str, Any],
    ) -> ParsedPicture | None:
        image_bytes, width, height = _picture_image(item, document)
        if image_bytes is None and (width <= 0 or height <= 0):
            return None
        caption = "\n".join(_caption_texts(document, item, item_map)).strip()
        return ParsedPicture(
            width=width,
            height=height,
            image_bytes=image_bytes,
            page_no=page_no,
            bbox=bbox,
            caption=caption,
            order=order,
        )

    @staticmethod
    def _extract_formula(
        item: Any, page_no: int | None, bbox, order: int
    ) -> ParsedFormula | None:
        text = _item_text(item).strip()
        latex: str | None = None
        enriched = getattr(item, "enriched_data", None)
        if enriched is not None:
            if isinstance(enriched, dict):
                value = enriched.get("latex")
                latex = str(value) if value else None
            else:
                value = getattr(enriched, "latex", None)
                latex = str(value) if value else None
        if not text and latex:
            text = latex
        if not text and not latex:
            return None
        return ParsedFormula(
            text=text,
            latex=latex,
            page_no=page_no,
            bbox=bbox,
            order=order,
        )

    def _merge_parts(
        self,
        parts: Sequence[ParsedDocument],
        source: Path,
        source_format: str,
    ) -> ParsedDocument:
        blocks: list[ParsedBlock] = []
        warnings: list[str] = []
        for part in parts:
            blocks.extend(part.blocks)
            warnings.extend(part.warnings)
        blocks.sort(key=lambda block: (block.page_no if block.page_no is not None else 0, block.order))
        for index, block in enumerate(blocks):
            block.order = index
            if block.table is not None:
                block.table.order = index
            if block.picture is not None:
                block.picture.order = index
            if block.formula is not None:
                block.formula.order = index
        return ParsedDocument(
            source_path=str(source),
            source_format=source_format,
            parser_backend=self.backend_name,
            blocks=blocks,
            warnings=warnings,
            page_semantics="physical",
            page_count=0,
        )


def legacy_to_pdf(
    converter: LibreOfficeConverter,
    source: str | Path,
    output_dir: str | Path,
) -> Path:
    """Convert a legacy ``.doc`` / ``.ppt`` file into a paginated PDF."""
    return converter.convert_to_pdf(source, output_dir=output_dir)


def pdf_page_total(path: str | Path) -> int:
    return pdf_page_count(path)
