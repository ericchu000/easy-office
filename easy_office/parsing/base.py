"""Pluggable document-parser contract and factory registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from easy_office.config.models import LocalParserConfig
from easy_office.parsing.models import ParsedDocument


class UnsupportedParserBackend(RuntimeError):
    """Raised when the configured parser backend is not registered."""


class DocumentParser(ABC):
    """Contract implemented by Docling and future MinerU/DeepSeek-OCR drivers."""

    backend_name: str = ""
    supported_formats: set[str] = set()

    @abstractmethod
    def parse(
        self,
        path: str | Path,
        *,
        workspace: str | Path | None = None,
    ) -> ParsedDocument:
        """Parse one file into backend-neutral blocks."""

    def supports(self, extension: str) -> bool:
        return extension.lower().lstrip(".") in self.supported_formats


ParserFactory = Callable[..., DocumentParser]


class ParserRegistry:
    """Maps backend names to factories without importing optional stacks eagerly."""

    def __init__(self) -> None:
        self._factories: dict[str, ParserFactory] = {}

    def register(self, name: str, factory: ParserFactory) -> None:
        key = name.strip().lower()
        if not key:
            raise ValueError("parser backend name must not be empty")
        self._factories[key] = factory

    def names(self) -> list[str]:
        return sorted(self._factories)

    def create(
        self,
        name: str,
        config: LocalParserConfig,
        *,
        weights_dir: str | Path | None = None,
        timeout_seconds: float = 180.0,
    ) -> DocumentParser:
        key = name.strip().lower()
        factory = self._factories.get(key)
        if factory is None:
            raise UnsupportedParserBackend(
                f"parser backend {name!r} is not registered; "
                f"available backends: {', '.join(self.names()) or '<none>'}"
            )
        return factory(
            config,
            weights_dir=weights_dir,
            timeout_seconds=timeout_seconds,
        )


def _docling_factory(
    config: LocalParserConfig,
    *,
    weights_dir: str | Path | None = None,
    timeout_seconds: float = 180.0,
):
    from easy_office.parsing.docling_parser import DoclingParser

    return DoclingParser(
        config,
        weights_dir=weights_dir,
        timeout_seconds=timeout_seconds,
    )


def default_registry() -> ParserRegistry:
    registry = ParserRegistry()
    registry.register("docling", _docling_factory)
    return registry
