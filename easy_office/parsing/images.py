"""Decorative-image filtering, cropping, OCR and VLM description.

The four-step chain from the product plan is implemented here:

1. locate and export the picture to the asset cache;
2. deterministically discard logos, icons, separators and repeated
   header/footer images;
3. OCR the retained image and ask the configured VLM for a description
   (falling back to OCR + dimensions when no VLM is configured);
4. the chunker later binds caption/neighbour context and the asset path.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Any

from easy_office.config.models import LocalParserConfig, VLMConfig
from easy_office.models.ocr import OcrError, RapidOcrEngine
from easy_office.models.services import VisionClient, VisionError
from easy_office.parsing.assets import AssetCache
from easy_office.parsing.models import ParsedDocument, ParsedPicture

logger = logging.getLogger(__name__)


@dataclass
class ImageProcessingStats:
    total: int = 0
    kept: int = 0
    filtered: int = 0
    vlm_described: int = 0
    ocr_used: int = 0
    filter_reasons: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "kept": self.kept,
            "filtered": self.filtered,
            "vlm_described": self.vlm_described,
            "ocr_used": self.ocr_used,
            "filter_reasons": dict(self.filter_reasons),
            "warnings": list(self.warnings),
        }


class ImageProcessor:
    """Applies deterministic filters and enriches retained pictures."""

    def __init__(
        self,
        config: LocalParserConfig,
        vlm_config: VLMConfig,
        assets: AssetCache,
        *,
        ocr: RapidOcrEngine | None = None,
        vlm: VisionClient | None = None,
    ) -> None:
        self._config = config
        self._vlm_config = vlm_config
        self._assets = assets
        self._ocr = ocr
        self._vlm = vlm

    def process(
        self, document: ParsedDocument, file_uid: str
    ) -> ImageProcessingStats:
        pictures = document.pictures
        stats = ImageProcessingStats(total=len(pictures))
        if not pictures:
            return stats
        page_sizes = {
            page.page_no: (page.width, page.height)
            for page in document.pages
            if page.width and page.height
        }
        scanned_pages = {page.page_no for page in document.pages if page.is_scanned}
        repeated = self._repeated_position_keys(pictures)
        for index, picture in enumerate(pictures):
            reason = self._decorative_reason(
                picture, page_sizes, scanned_pages, repeated
            )
            if reason is not None:
                picture.filtered_reason = reason
                # Release the bytes of discarded decorations early.
                picture.image_bytes = None
                stats.filtered += 1
                stats.filter_reasons[reason] = stats.filter_reasons.get(reason, 0) + 1
                continue
            stats.kept += 1
            payload = picture.image_bytes
            if not payload:
                picture.filtered_reason = "no_pixels"
                stats.filtered += 1
                stats.kept -= 1
                stats.filter_reasons["no_pixels"] = stats.filter_reasons.get("no_pixels", 0) + 1
                continue
            try:
                asset_path = self._assets.write_bytes(
                    file_uid,
                    "picture",
                    payload,
                    page_no=picture.page_no,
                    index=index,
                    extension="png",
                )
                picture.asset_path = str(asset_path)
            except OSError as exc:
                stats.warnings.append(
                    f"image asset could not be written (page {picture.page_no}): {exc}"
                )
            self._extract_ocr(picture, stats)
            self._describe(picture, stats)
        return stats

    # --------------------------------------------------------------- filtering
    @staticmethod
    def _position_key(picture: ParsedPicture) -> tuple[int, int, int, int] | None:
        if picture.bbox is None:
            return None
        x0, y0, x1, y1 = picture.bbox
        return (
            round(x0 / 8.0),
            round(y0 / 8.0),
            round((x1 - x0) / 8.0),
            round((y1 - y0) / 8.0),
        )

    def _repeated_position_keys(
        self, pictures: list[ParsedPicture]
    ) -> dict[tuple[int, int, int, int], set[int]]:
        seen: dict[tuple[int, int, int, int], set[int]] = {}
        for picture in pictures:
            key = self._position_key(picture)
            if key is None or picture.page_no is None:
                continue
            seen.setdefault(key, set()).add(int(picture.page_no))
        return {key: pages for key, pages in seen.items() if len(pages) >= 2}

    def _decorative_reason(
        self,
        picture: ParsedPicture,
        page_sizes: dict[int, tuple[float, float]],
        scanned_pages: set[int],
        repeated: dict[tuple[int, int, int, int], set[int]],
    ) -> str | None:
        width, height = int(picture.width), int(picture.height)
        if width <= 0 or height <= 0:
            return "no_pixels"
        if width < self._config.image_min_side_px or height < self._config.image_min_side_px:
            return "small_side"
        if width * height < self._config.image_min_area_px:
            return "small_area"
        longer = max(width, height)
        shorter = max(1, min(width, height))
        if longer / shorter > self._config.image_max_aspect_ratio:
            return "extreme_aspect"
        if picture.bbox is not None and picture.page_no in page_sizes:
            page_width, page_height = page_sizes[picture.page_no]
            area = page_width * page_height
            if area > 0:
                box_area = max(0.0, picture.bbox[2] - picture.bbox[0]) * max(
                    0.0, picture.bbox[3] - picture.bbox[1]
                )
                coverage = box_area / area
                if picture.page_no in scanned_pages:
                    if coverage >= self._config.scanned_page_image_coverage:
                        return "page_image"
                elif coverage >= self._config.image_max_page_coverage:
                    return "page_image"
        key = self._position_key(picture)
        if key is not None and key in repeated and picture.bbox is not None:
            page_height = page_sizes.get(picture.page_no or 0, (0.0, 0.0))[1]
            if page_height:
                center_y = (picture.bbox[1] + picture.bbox[3]) / 2.0
                if center_y <= page_height * 0.15 or center_y >= page_height * 0.85:
                    return "repeated_header_footer"
        return None

    # ---------------------------------------------------------------- content
    def _extract_ocr(self, picture: ParsedPicture, stats: ImageProcessingStats) -> None:
        if self._ocr is None or not picture.image_bytes:
            return
        try:
            result = self._ocr.recognize(picture.image_bytes)
        except OcrError as exc:
            stats.warnings.append(f"OCR failed on page {picture.page_no}: {exc}")
            return
        if not result.is_empty:
            picture.ocr_text = result.text
            stats.ocr_used += 1

    def _describe(self, picture: ParsedPicture, stats: ImageProcessingStats) -> None:
        if self._vlm is None or not picture.image_bytes:
            self._fallback_description(picture)
            return
        payload = self._resize_for_vlm(picture.image_bytes)
        instruction_parts: list[str] = []
        if picture.caption:
            instruction_parts.append(f"Caption: {picture.caption}")
        if picture.ocr_text:
            instruction_parts.append(f"OCR text: {picture.ocr_text[:1500]}")
        try:
            description = self._vlm.describe_image(
                payload,
                mime_type="image/png",
                extra_instruction="\n".join(instruction_parts),
            )
        except VisionError as exc:
            stats.warnings.append(
                f"VLM description failed on page {picture.page_no}: {exc}"
            )
            self._fallback_description(picture)
            return
        except Exception as exc:  # pragma: no cover - defensive network layer
            logger.exception("unexpected VLM failure")
            stats.warnings.append(
                f"VLM description failed on page {picture.page_no}: {exc}"
            )
            self._fallback_description(picture)
            return
        picture.description = description.detailed_caption or (
            f"{description.scene_category} {description.subject_and_portrait}"
        ).strip()
        picture.description_source = "vlm"
        picture.visual_tags = list(description.visual_tags)
        stats.vlm_described += 1
        if not picture.description:
            self._fallback_description(picture)

    def _fallback_description(self, picture: ParsedPicture) -> None:
        if picture.description:
            return
        parts = [f"图片（{picture.width}×{picture.height} 像素）"]
        if picture.caption:
            parts.append(f"图注：{picture.caption}")
        picture.description = "；".join(parts)
        picture.description_source = "fallback"

    def _resize_for_vlm(self, payload: bytes) -> bytes:
        limit = int(self._vlm_config.max_image_resolution)
        try:
            from PIL import Image  # type: ignore

            image = Image.open(io.BytesIO(payload))
            if max(image.width, image.height) <= limit:
                return payload
            ratio = limit / float(max(image.width, image.height))
            size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
            resized = image.convert("RGB").resize(size)
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG")
            return buffer.getvalue()
        except Exception:
            return payload
