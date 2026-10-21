"""Model-service adapters (embeddings, vision, OCR)."""

from easy_office.models.ocr import (
    OcrBlock,
    OcrError,
    OcrResult,
    OcrUnavailable,
    RapidOcrEngine,
    build_ocr_engine,
)
from easy_office.models.services import (
    EmbeddingClient,
    EmbeddingError,
    ModelServiceError,
    VisionClient,
    VisionDescription,
    VisionError,
    build_embedding_client,
    build_vision_client,
)

__all__ = [
    "EmbeddingClient",
    "EmbeddingError",
    "ModelServiceError",
    "OcrBlock",
    "OcrError",
    "OcrResult",
    "OcrUnavailable",
    "RapidOcrEngine",
    "VisionClient",
    "VisionDescription",
    "VisionError",
    "build_embedding_client",
    "build_ocr_engine",
    "build_vision_client",
]
