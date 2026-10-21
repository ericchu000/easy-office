"""Deterministic office-document structure summary.

Everything here is computed from parser output and page probes; no model call
is involved, so the summary is reproducible and cheap.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from easy_office.parsing.models import ParsedDocument, ParsedPicture
from easy_office.parsing.text_signals import looks_like_formula

_HAN = re.compile(r"[一-鿿㐀-䶿]")
_HIRAGANA = re.compile(r"[぀-ゟ]")
_KATAKANA = re.compile(r"[゠-ヿ]")
_HANGUL = re.compile(r"[가-힯]")
_LATIN = re.compile(r"[A-Za-z]")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_ARABIC = re.compile(r"[؀-ۿ]")

_SCRIPT_PATTERNS = (
    ("han", _HAN),
    ("hiragana", _HIRAGANA),
    ("katakana", _KATAKANA),
    ("hangul", _HANGUL),
    ("latin", _LATIN),
    ("cyrillic", _CYRILLIC),
    ("arabic", _ARABIC),
)


def language_profile(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for name, pattern in _SCRIPT_PATTERNS:
        counts[name] = len(pattern.findall(text))
    return counts


def detect_language(text: str) -> str:
    """Return a dominant ISO-639-1-ish language code.

    This is an intentional deterministic heuristic rather than a language
    detection dependency: it only needs to answer "what is this document mostly
    written in" for filtering and labels.
    """
    counts = language_profile(text)
    if not sum(counts.values()):
        return "unknown"
    kana = counts["hiragana"] + counts["katakana"]
    cjk = counts["han"] + kana + counts["hangul"]
    latin = counts["latin"]
    if counts["hangul"] and counts["hangul"] >= max(cjk - counts["hangul"], latin):
        return "ko"
    if kana and kana >= max(counts["han"], latin) * 0.5:
        return "ja"
    if counts["han"] and counts["han"] >= latin * 0.1:
        return "zh"
    if latin:
        return "en"
    dominant = max(
        ("ru", "cyrillic"),
        ("ar", "arabic"),
        key=lambda item: counts[item[1]],
    )
    return dominant[0] if counts[dominant[1]] else "unknown"


def detect_languages(text: str) -> list[str]:
    """Language components that carry a meaningful share of the text."""
    counts = language_profile(text)
    total = sum(counts.values())
    if not total:
        return []
    result: list[str] = []
    han = counts["han"]
    kana = counts["hiragana"] + counts["katakana"]
    if han and han / total >= 0.1:
        result.append("zh")
    if kana and kana / total >= 0.1:
        result.append("ja")
    if counts["hangul"] and counts["hangul"] / total >= 0.1:
        result.append("ko")
    if counts["latin"] and counts["latin"] / total >= 0.1:
        result.append("en")
    if counts["cyrillic"] and counts["cyrillic"] / total >= 0.1:
        result.append("ru")
    if counts["arabic"] and counts["arabic"] / total >= 0.1:
        result.append("ar")
    return result or [detect_language(text)]


def carrier_type(document: ParsedDocument) -> str:
    if document.page_semantics != "physical" or not document.pages:
        return "digital"
    scanned = sum(1 for page in document.pages if page.is_scanned)
    if scanned == 0:
        return "digital"
    if scanned == len(document.pages):
        return "scanned"
    return "mixed"


def _looks_like_formula(text: str) -> bool:
    return looks_like_formula(text)


def build_office_summary(
    document: ParsedDocument,
    *,
    pictures: Iterable[ParsedPicture] | None = None,
    extra_warnings: Iterable[str] | None = None,
) -> dict:
    """Build the ``office`` modality summary stored on the files table."""
    all_pictures = list(pictures) if pictures is not None else document.pictures
    kept_pictures = [picture for picture in all_pictures if picture.kept]
    tables = [table for table in document.tables if not table.is_empty]
    formulas = [formula for formula in document.formulas if formula.display_text]

    if document.page_semantics == "flow":
        total_pages = 1
        slide_count = None
    elif document.page_semantics == "slide":
        slide_count = document.page_count or max(
            (page.page_no for page in document.pages), default=0
        )
        total_pages = slide_count
    else:
        total_pages = document.page_count or len(document.pages)
        slide_count = None

    text = document.text_sample()
    formula_like_blocks = sum(
        1 for block in document.blocks if block.kind in {"text", "list"} and _looks_like_formula(block.text)
    )
    formula_hint_pages = sum(1 for page in document.pages if page.formula_hint)
    heuristic_formulas = not formulas and formula_like_blocks > 0
    warnings = [*document.warnings, *(extra_warnings or [])]
    summary: dict = {
        "total_pages": int(total_pages),
        "total_characters": int(document.textual_char_count()),
        "primary_language": detect_language(text),
        "languages": detect_languages(text),
        "carrier_type": carrier_type(document),
        "has_tables": bool(tables),
        "has_formulas": bool(formulas) or heuristic_formulas or formula_hint_pages > 0,
        "has_charts_or_images": bool(kept_pictures),
        "formula_detection": (
            "parser"
            if formulas
            else ("native_text" if formula_hint_pages else ("heuristic" if heuristic_formulas else "none"))
        ),
        "formula_like_blocks": int(formula_like_blocks),
        "formula_hint_pages": int(formula_hint_pages),
        "table_count": len(tables),
        "formula_count": len(formulas),
        "image_count": len(kept_pictures),
        "filtered_image_count": len(all_pictures) - len(kept_pictures),
        "parser_backend": document.parser_backend,
        "source_format": document.source_format,
        "page_semantics": document.page_semantics,
        "warnings": warnings,
    }
    if slide_count is not None:
        summary["slide_count"] = int(slide_count)
    if document.converted_pdf_path:
        summary["rendered_via"] = "libreoffice_pdf"
    return summary
