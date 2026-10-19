"""Jieba-based bilingual tokenization for FTS5.

SQLite's bundled ``unicode61`` tokenizer treats a whole run of Chinese
characters as a single token. We therefore segment text with jieba *before*
writing it to FTS5 and segment queries the same way, which gives meaningful
BM25 term frequencies for both Chinese and English.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import unicodedata
from typing import Any

_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_TOKEN_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+|[A-Za-z0-9_]+")
_TAG = re.compile(r"<[^>]+>")
_MARKUP = re.compile(r"</?[A-Za-z][^>]*>")
_WHITESPACE = re.compile(r"\s+")

_jieba_module: Any = None
_jieba_lock = threading.Lock()


def _jieba() -> Any:
    global _jieba_module
    if _jieba_module is None:
        with _jieba_lock:
            if _jieba_module is None:
                try:
                    import jieba as module
                except ImportError as exc:  # pragma: no cover - environment failure
                    raise RuntimeError(
                        "jieba is required for Chinese full-text indexing. "
                        "Install it with: uv add jieba"
                    ) from exc
                module.setLogLevel(logging.ERROR)
                _jieba_module = module
    return _jieba_module


def initialize() -> None:
    """Eagerly initialize jieba's dictionary (useful to warm the daemon)."""
    _jieba().initialize()


def tokenize(text: str) -> list[str]:
    """Split mixed Chinese/English text into lowercase search terms."""
    if not text:
        return []
    normalized = unicodedata.normalize("NFKC", str(text)).lower()
    tokens: list[str] = []
    for run in _TOKEN_RUN.findall(normalized):
        if _CJK_RUN.fullmatch(run):
            tokens.extend(
                piece.strip()
                for piece in _jieba().cut_for_search(run)
                if piece and piece.strip()
            )
        else:
            tokens.append(run)
    return tokens


def normalize_for_index(text: str) -> str:
    """Return the space-separated token stream stored in FTS5's body column."""
    return " ".join(tokenize(text))


def contains_markup(text: str) -> bool:
    """Heuristically detect HTML/XML markup in a text fragment."""
    return bool(text) and bool(_MARKUP.search(text))


def strip_html(text: str) -> str:
    """Remove tags/entities so HTML tables index as readable text."""
    if not text:
        return ""
    without_tags = _TAG.sub(" ", text)
    return _WHITESPACE.sub(" ", html.unescape(without_tags)).strip()


def plain_text_for_index(text: str) -> str:
    """Return markup-free text suitable for FTS and mirror files."""
    return strip_html(text) if contains_markup(text) else (text or "")


def _quote_term(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def build_match_expression(text: str, *, operator: str = "OR", max_terms: int = 64) -> str | None:
    """Build an FTS5 MATCH expression from a natural-language query."""
    op = operator.upper()
    if op not in {"OR", "AND"}:
        raise ValueError("operator must be 'OR' or 'AND'")
    unique_terms = list(dict.fromkeys(tokenize(text)))[:max_terms]
    if not unique_terms:
        return None
    return f" {op} ".join(_quote_term(term) for term in unique_terms)


def make_snippet(text: str, tokens: list[str], *, width: int = 180) -> str:
    """Return a compact excerpt around the first matching query term."""
    collapsed = _WHITESPACE.sub(" ", text or "").strip()
    if not collapsed:
        return ""
    lowered = collapsed.lower()
    earliest = -1
    for token in tokens:
        if not token:
            continue
        position = lowered.find(token.lower())
        if position >= 0 and (earliest < 0 or position < earliest):
            earliest = position
    if earliest < 0:
        return collapsed[:width] + ("…" if len(collapsed) > width else "")
    half = max(20, width // 2)
    start = max(0, earliest - half)
    end = min(len(collapsed), earliest + half)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(collapsed) else ""
    return f"{prefix}{collapsed[start:end]}{suffix}"
