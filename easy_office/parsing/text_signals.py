"""Small deterministic text signals shared by probes and summaries."""

from __future__ import annotations

import re

# LaTeX commands, Unicode math symbols, optimisation operators, or a simple
# variable = expression pattern. This is a signal, not a parser: it decides
# whether a page/document *looks like* it contains formulas so the summary
# flag and the optional formula-enrichment route can make an informed choice.
FORMULA_PATTERN = re.compile(
    r"(?:\\[A-Za-z]+)|[∑∫√≤≥≈≠∂∇±×÷]|"
    r"\b(?:arg\s*min|arg\s*max)\b|"
    r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9_{}^]*\s*=\s*[^=\n]{1,80}",
    re.IGNORECASE,
)


def looks_like_formula(text: str) -> bool:
    return bool(FORMULA_PATTERN.search(text or ""))
