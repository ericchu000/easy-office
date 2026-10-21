"""Standalone OCR adapter for cropped document images.

Docling uses RapidOCR internally for scanned PDF pages. Cropped pictures need
the same engine without constructing a whole Docling pipeline, so this module
wraps RapidOCR directly. Both the legacy ``rapidocr_onnxruntime`` package and
the newer ``rapidocr`` package are supported.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Any

from easy_office.config.models import LocalParserConfig

logger = logging.getLogger(__name__)

# RapidOCR logs every model path at INFO and reports empty detections at
# WARNING on each construction/call. Both are noise for batch indexing.
logging.getLogger("RapidOCR").setLevel(logging.ERROR)


def _quiet_rapidocr() -> None:
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)


class OcrError(RuntimeError):
    """Base class for OCR failures."""


class OcrUnavailable(OcrError):
    """Raised when the configured OCR engine is not installed."""


@dataclass
class OcrBlock:
    text: str
    bbox: tuple[float, float, float, float] | None = None
    score: float | None = None


@dataclass
class OcrResult:
    text: str
    blocks: list[OcrBlock] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def _box_bounds(box: Any) -> tuple[float, float, float, float] | None:
    try:
        points = list(box)
        if not points:
            return None
        if len(points) == 4 and all(isinstance(item, (int, float)) for item in points):
            return (float(points[0]), float(points[1]), float(points[2]), float(points[3]))
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        return (min(xs), min(ys), max(xs), max(ys))
    except (TypeError, ValueError, IndexError):
        return None


def _load_engine():
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        _quiet_rapidocr()
        return RapidOCR, "rapidocr_onnxruntime"
    except ImportError:
        pass
    try:
        from rapidocr import RapidOCR  # type: ignore

        _quiet_rapidocr()
        return RapidOCR, "rapidocr"
    except ImportError as exc:
        raise OcrUnavailable(
            "RapidOCR is not installed. Install it with: "
            'uv add "docling[rapidocr]"'
        ) from exc


class RapidOcrEngine:
    """Thin deterministic wrapper around RapidOCR."""

    def __init__(self, languages: list[str] | None = None) -> None:
        engine_class, package = _load_engine()
        self._package = package
        _quiet_rapidocr()
        # RapidOCR 3.x reads its console log level from Global.log_level and
        # resets the logger during construction. Older packages do not accept
        # ``params``, hence the fallback.
        try:
            self._engine = engine_class(params={"Global.log_level": "error"})
        except (TypeError, ValueError, KeyError):
            self._engine = engine_class()
        _quiet_rapidocr()

    @property
    def package(self) -> str:
        return self._package

    def recognize(self, image: bytes | Any) -> OcrResult:
        pil_image = self._to_image(image)
        try:
            import numpy as np  # type: ignore
        except ImportError as exc:  # pragma: no cover - numpy ships with docling
            raise OcrError("numpy is required for OCR") from exc
        array = np.array(pil_image.convert("RGB"))
        try:
            raw = self._engine(array)
        except Exception as exc:  # pragma: no cover - engine-internal failure
            raise OcrError(f"OCR engine failed: {exc}") from exc
        blocks = self._normalize(raw)
        blocks.sort(
            key=lambda block: (
                (block.bbox[1] + block.bbox[3]) / 2 if block.bbox else 0.0,
                block.bbox[0] if block.bbox else 0.0,
            )
        )
        text = "\n".join(block.text for block in blocks if block.text.strip())
        return OcrResult(text=text, blocks=blocks)

    def _normalize(self, raw: Any) -> list[OcrBlock]:
        # Legacy API: (result_list, elapsed) where each row is [box, text, score]
        payload = raw
        if isinstance(raw, tuple) and len(raw) == 2:
            payload = raw[0]
        # New API: object with .txts / .boxes / .scores
        if hasattr(payload, "txts"):
            raw_texts = getattr(payload, "txts", None)
            raw_boxes = getattr(payload, "boxes", None)
            raw_scores = getattr(payload, "scores", None)
            texts = list(raw_texts) if raw_texts is not None else []
            boxes = list(raw_boxes) if raw_boxes is not None else []
            scores = list(raw_scores) if raw_scores is not None else []
            blocks: list[OcrBlock] = []
            for index, text in enumerate(texts):
                blocks.append(
                    OcrBlock(
                        text=str(text or ""),
                        bbox=_box_bounds(boxes[index]) if index < len(boxes) else None,
                        score=float(scores[index]) if index < len(scores) else None,
                    )
                )
            return blocks
        if not isinstance(payload, (list, tuple)):
            return []
        blocks = []
        for row in payload:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            blocks.append(
                OcrBlock(
                    text=str(row[1] or ""),
                    bbox=_box_bounds(row[0]),
                    score=float(row[2]) if len(row) > 2 and row[2] is not None else None,
                )
            )
        return blocks

    @staticmethod
    def _to_image(image: bytes | Any):
        try:
            from PIL import Image  # type: ignore
        except ImportError as exc:  # pragma: no cover - docling dependency
            raise OcrError("Pillow is required for OCR") from exc
        if isinstance(image, (bytes, bytearray)):
            return Image.open(io.BytesIO(bytes(image)))
        return image


def build_ocr_engine(config: LocalParserConfig) -> RapidOcrEngine | None:
    """Return an OCR engine only when enabled and importable."""
    if not config.ocr_enabled:
        return None
    if config.ocr_engine.strip().lower() not in ("rapidocr", "rapidocr_onnxruntime", "auto"):
        return None
    try:
        return RapidOcrEngine(config.ocr_languages)
    except OcrUnavailable:
        return None
