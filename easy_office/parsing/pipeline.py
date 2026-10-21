"""End-to-end office ingestion pipeline.

Responsible for: choosing the parser backend, converting legacy formats,
routing PDFs, filtering/describing images, building chunks, computing
embeddings, writing the hybrid index and backfilling the office summary.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from easy_office.models.ocr import build_ocr_engine
from easy_office.models.services import build_embedding_client, build_vision_client
from easy_office.parsing.assets import AssetCache
from easy_office.parsing.base import ParserRegistry, default_registry
from easy_office.parsing.chunker import OfficeChunker
from easy_office.parsing.docling_parser import DocumentParseError
from easy_office.parsing.images import ImageProcessingStats, ImageProcessor
from easy_office.parsing.libreoffice import (
    LEGACY_EXTENSIONS,
    LibreOfficeConverter,
    LibreOfficeError,
    discover_libreoffice,
)
from easy_office.parsing.summary import build_office_summary

logger = logging.getLogger(__name__)

OFFICE_EXTENSIONS = {"pdf", "docx", "pptx", "doc", "ppt"}


class OfficeIngestError(RuntimeError):
    """Raised when an office document cannot be fully ingested."""


@dataclass
class OfficeIngestResult:
    file_uid: str
    file_name: str
    source_format: str
    parser_backend: str
    page_count: int
    chunk_count: int
    embedded_chunk_count: int
    table_count: int
    image_count: int
    filtered_image_count: int
    formula_count: int
    carrier_type: str
    page_semantics: str
    elapsed_seconds: float
    summary: dict[str, Any] = field(default_factory=dict)
    image_stats: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_uid": self.file_uid,
            "file_name": self.file_name,
            "source_format": self.source_format,
            "parser_backend": self.parser_backend,
            "page_count": self.page_count,
            "chunk_count": self.chunk_count,
            "embedded_chunk_count": self.embedded_chunk_count,
            "table_count": self.table_count,
            "image_count": self.image_count,
            "filtered_image_count": self.filtered_image_count,
            "formula_count": self.formula_count,
            "carrier_type": self.carrier_type,
            "page_semantics": self.page_semantics,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "summary": dict(self.summary),
            "image_stats": dict(self.image_stats),
            "warnings": list(self.warnings),
        }


class OfficeDocumentPipeline:
    """Coordinates the Step 2 office-document path on top of an EasyOfficeCore."""

    def __init__(self, core: Any, *, registry: ParserRegistry | None = None) -> None:
        self._core = core
        self._registry = registry or default_registry()
        self._parser = None
        self._parser_key: tuple[str, str, float, str] | None = None
        self._parser_lock = threading.RLock()

    @property
    def registry(self) -> ParserRegistry:
        return self._registry

    @property
    def supported_extensions(self) -> set[str]:
        return set(OFFICE_EXTENSIONS)

    def supports(self, path_or_extension: str | Path) -> bool:
        text = str(path_or_extension).lower()
        extension = text.rsplit(".", 1)[-1] if "." in text else text
        return extension.lstrip(".") in OFFICE_EXTENSIONS

    # ------------------------------------------------------------------ ingest
    def ingest_path(self, path: str | Path) -> OfficeIngestResult:
        record = self._core.register_inplace(path)
        return self.ingest(record.file_uid)

    async def ingest_path_async(self, path: str | Path) -> OfficeIngestResult:
        return await asyncio.to_thread(self.ingest_path, path)

    async def ingest_async(self, file_uid: str) -> OfficeIngestResult:
        return await asyncio.to_thread(self.ingest, file_uid)

    def ingest(self, file_uid: str) -> OfficeIngestResult:
        started = time.monotonic()
        record = self._core.metadata.get_file(file_uid)
        if record is None:
            raise OfficeIngestError(f"unknown file: {file_uid}")
        extension = (record.extension or "").lower().lstrip(".")
        if extension not in OFFICE_EXTENSIONS:
            raise OfficeIngestError(
                f"step 2 handles office documents only; got .{extension or '?'}"
            )
        source = Path(record.local_path)
        if not source.is_file():
            raise OfficeIngestError(f"source file is missing: {source}")

        config = self._core.config.get()
        paths = self._core.config.paths()
        assets: AssetCache = self._core.assets
        workspace = assets.temp_workspace(file_uid)
        try:
            if extension in LEGACY_EXTENSIONS:
                # LibreOffice occasionally crashes on Windows when the parent
                # process is already holding Docling's PyTorch models. Release
                # them before conversion, then recreate the parser for the
                # rendered PDF.
                self._release_parser()
                converted = self._convert_legacy(source, workspace, config)
                parser = self._get_parser(config, paths)
                with self._parser_lock:
                    parsed = parser.parse(converted, workspace=workspace)
                parsed.source_path = str(source)
                parsed.source_format = extension
                parsed.converted_pdf_path = str(converted)
            else:
                converted = None
                parser = self._get_parser(config, paths)
                with self._parser_lock:
                    parsed = parser.parse(source, workspace=workspace)

            # Only clear old cropped assets after a successful parse, so a
            # failed re-index leaves the previous generation intact.
            assets.clear(file_uid)
            image_stats = self._process_images(parsed, file_uid, config)
            warnings = list(parsed.warnings) + list(image_stats.warnings)
            if converted is not None:
                warnings.append(f"legacy format rendered via LibreOffice: {converted.name}")

            summary = build_office_summary(
                parsed,
                pictures=parsed.pictures,
                extra_warnings=warnings,
            )
            chunks = OfficeChunker(config.local_parser).build(parsed)
            if not chunks:
                raise OfficeIngestError(
                    f"parser produced no indexable chunks for {record.file_name}"
                )
            embedded = self._embed_chunks(chunks, config)
            self._core.index.replace_file_chunks(file_uid, chunks)
            self._core.metadata.update_modality_summary(file_uid, "office", summary)
            elapsed = time.monotonic() - started
            return OfficeIngestResult(
                file_uid=file_uid,
                file_name=record.file_name,
                source_format=parsed.source_format,
                parser_backend=parsed.parser_backend,
                page_count=parsed.page_count,
                chunk_count=len(chunks),
                embedded_chunk_count=embedded,
                table_count=len(parsed.tables),
                image_count=sum(1 for picture in parsed.pictures if picture.kept),
                filtered_image_count=image_stats.filtered,
                formula_count=len(parsed.formulas),
                carrier_type=str(summary.get("carrier_type", "")),
                page_semantics=parsed.page_semantics,
                elapsed_seconds=elapsed,
                summary=summary,
                image_stats=image_stats.to_dict(),
                warnings=warnings,
            )
        except (LibreOfficeError, DocumentParseError, OfficeIngestError):
            raise
        except Exception as exc:
            raise OfficeIngestError(
                f"failed to ingest {record.file_name}: {exc}"
            ) from exc
        finally:
            AssetCache.remove_workspace(workspace)

    # ---------------------------------------------------------------- internals
    def _get_parser(self, config, paths):
        """Reuse one parser (and its loaded models) until configuration changes."""
        key = (
            config.storage.parsing_backend.strip().lower(),
            str(paths.weights_dir),
            float(config.concurrency.file_timeout_seconds),
            config.local_parser.model_dump_json(),
        )
        with self._parser_lock:
            if self._parser is None or key != self._parser_key:
                self._parser = self._registry.create(
                    config.storage.parsing_backend,
                    config.local_parser,
                    weights_dir=paths.weights_dir,
                    timeout_seconds=config.concurrency.file_timeout_seconds,
                )
                self._parser_key = key
            return self._parser

    def _release_parser(self) -> None:
        with self._parser_lock:
            parser = self._parser
            self._parser = None
            self._parser_key = None
        if parser is not None:
            close = getattr(parser, "close", None)
            if callable(close):
                close()
            del parser
        gc.collect()

    def _convert_legacy(self, source: Path, workspace: Path, config) -> Path:
        executable = discover_libreoffice(config.local_parser.libreoffice_path)
        converter = LibreOfficeConverter(
            executable, timeout_seconds=config.concurrency.file_timeout_seconds
        )
        return converter.convert_to_pdf(source, output_dir=workspace)

    def _process_images(self, parsed, file_uid: str, config) -> ImageProcessingStats:
        ocr = build_ocr_engine(config.local_parser)
        vlm = build_vision_client(config.vlm)
        processor = ImageProcessor(
            config.local_parser,
            config.vlm,
            self._core.assets,
            ocr=ocr,
            vlm=vlm,
        )
        return processor.process(parsed, file_uid)

    @staticmethod
    def _embed_chunks(chunks, config) -> int:
        client = build_embedding_client(config.embedding)
        if client is None:
            return 0
        vectors = client.embed_texts([chunk.plain_text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise OfficeIngestError(
                f"embedding count mismatch: {len(vectors)} vectors for {len(chunks)} chunks"
            )
        for chunk, vector in zip(chunks, vectors):
            chunk.embedding = vector
        return len(vectors)
