"""Strongly typed configuration models.

Every section is a Pydantic model with ``extra="forbid"`` so typos and illegal
values are rejected before anything is persisted or applied.
"""

from __future__ import annotations

import copy
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

MASK = "********"
PLATFORM_NAMES = ("telegram", "wecom", "feishu", "dingtalk")


class ServiceMode(str, Enum):
    """How a model service is reached."""

    OPENAI_COMPATIBLE = "openai_compatible"
    OLLAMA = "ollama"
    LOCAL = "local"
    DISABLED = "disabled"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)


class _ServiceConfig(_StrictModel):
    provider: ServiceMode = ServiceMode.OPENAI_COMPATIBLE
    base_url: str = ""
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        if not text.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return text.rstrip("/")


class LLMConfig(_ServiceConfig):
    """Text reasoning / chat model used by the agent loop and extraction."""

    model_name: str = ""
    max_context_window: int = Field(default=8192, ge=512, le=2_000_000)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)


class VLMConfig(_ServiceConfig):
    """Vision-language model used for image understanding and verification."""

    model_name: str = ""
    max_image_resolution: int = Field(default=2048, ge=256, le=16384)


class EmbeddingConfig(_ServiceConfig):
    """Text embedding model used for chunk and query vectorization."""

    model_name: str = ""
    embedding_dim: int = Field(default=1024, ge=1, le=16384)
    batch_size: int = Field(default=32, ge=1, le=2048)


class RerankerConfig(_ServiceConfig):
    """Cross-encoder reranker for hybrid retrieval results."""

    enabled: bool = False
    model_name: str = ""
    top_n: int = Field(default=20, ge=1, le=1000)


class LocalParserConfig(_StrictModel):
    """Docling / OCR local weights and hardware acceleration settings."""

    weights_dir: str = "models"
    device: Literal["cpu", "cuda", "mps"] = "cpu"
    layout_model: str = "docling-layout"
    table_model: str = "docling-tableformer"
    ocr_engine: str = "rapidocr"
    ocr_languages: list[str] = Field(default_factory=lambda: ["ch_sim", "en"])
    ocr_enabled: bool = True

    @field_validator("weights_dir", "layout_model", "table_model", "ocr_engine")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("value must not be empty")
        if "\x00" in text:
            raise ValueError("value must not contain NUL characters")
        return text

    @field_validator("ocr_languages")
    @classmethod
    def _validate_languages(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("ocr_languages must contain at least one language")
        return cleaned


class StorageConfig(_StrictModel):
    """Paths and driver selection for both physical storage modes."""

    host_watch_dirs: list[str] = Field(default_factory=list)
    host_allowed_roots: list[str] = Field(default_factory=list)
    managed_workspace: str = "managed"
    index_db_path: str = "index/knowledge.db"
    mirror_cache_dir: str = "mirror"
    parsing_backend: str = "docling"
    vector_store_driver: str = "sqlite"
    object_store_driver: str = "local"
    mirror_enabled: bool = True
    max_upload_bytes: int = Field(default=4 * 1024 * 1024 * 1024, ge=1)

    @field_validator(
        "managed_workspace",
        "index_db_path",
        "mirror_cache_dir",
        "parsing_backend",
        "vector_store_driver",
        "object_store_driver",
    )
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("value must not be empty")
        if "\x00" in text:
            raise ValueError("value must not contain NUL characters")
        return text

    @field_validator("host_watch_dirs", "host_allowed_roots")
    @classmethod
    def _validate_path_list(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip()
            if not text:
                continue
            if "\x00" in text:
                raise ValueError("paths must not contain NUL characters")
            cleaned.append(text)
        return cleaned


class ConcurrencyConfig(_StrictModel):
    """Multiprocess indexing and batching controls."""

    worker_processes: int = Field(default=4, ge=1, le=256)
    index_batch_size: int = Field(default=64, ge=1, le=4096)
    file_timeout_seconds: float = Field(default=300.0, gt=0.0, le=86400.0)


class GatewayPlatformConfig(_StrictModel):
    """Credentials and access control for one IM platform connector."""

    enabled: bool = False
    app_id: str = ""
    app_secret: SecretStr = Field(default_factory=lambda: SecretStr(""))
    token: SecretStr = Field(default_factory=lambda: SecretStr(""))
    allowed_user_ids: list[str] = Field(default_factory=list)

    @field_validator("allowed_user_ids")
    @classmethod
    def _validate_users(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]


def _default_platforms() -> dict[str, GatewayPlatformConfig]:
    return {name: GatewayPlatformConfig() for name in PLATFORM_NAMES}


class GatewayConfig(_StrictModel):
    """External message gateway configuration (Step 12 consumes it)."""

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    webhook_base_url: str = ""
    redact_logs: bool = True
    platforms: dict[str, GatewayPlatformConfig] = Field(default_factory=_default_platforms)

    @field_validator("platforms")
    @classmethod
    def _validate_platforms(
        cls, value: dict[str, GatewayPlatformConfig]
    ) -> dict[str, GatewayPlatformConfig]:
        unknown = sorted(set(value) - set(PLATFORM_NAMES))
        if unknown:
            raise ValueError(f"unknown gateway platforms: {', '.join(unknown)}")
        normalized = dict(value)
        for name in PLATFORM_NAMES:
            normalized.setdefault(name, GatewayPlatformConfig())
        return normalized

    @field_validator("webhook_base_url")
    @classmethod
    def _validate_webhook(cls, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        if not text.startswith(("http://", "https://")):
            raise ValueError("webhook_base_url must start with http:// or https://")
        return text.rstrip("/")


class AppConfig(_StrictModel):
    """Root configuration object."""

    version: int = Field(default=1, ge=1)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    vlm: VLMConfig = Field(default_factory=VLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    reranker: RerankerConfig = Field(default_factory=RerankerConfig)
    local_parser: LocalParserConfig = Field(default_factory=LocalParserConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> "AppConfig":
        if self.reranker.enabled and self.reranker.provider == ServiceMode.DISABLED:
            raise ValueError("reranker.enabled cannot be true when provider is disabled")
        return self


def _to_plain(value: Any) -> Any:
    """Convert Pydantic models to plain Python structures, exposing secrets."""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseModel):
        return {
            name: _to_plain(getattr(value, name))
            for name in type(value).model_fields
        }
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(item) for item in value]
    return value


def iter_secret_paths(value: Any, prefix: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    """Yield the structural path of every SecretStr inside a model tree."""
    if isinstance(value, SecretStr):
        yield prefix
        return
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from iter_secret_paths(getattr(value, name), prefix + (name,))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from iter_secret_paths(item, prefix + (key,))
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from iter_secret_paths(item, prefix + (index,))


def _get_path(value: Any, path: tuple[Any, ...]) -> Any:
    current = value
    for key in path:
        current = current[key]
    return current


def _set_path(value: Any, path: tuple[Any, ...], new_value: Any) -> None:
    current = value
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = new_value


def to_raw_dict(config: BaseModel) -> dict[str, Any]:
    """Plain dict with real secret values, suitable for YAML persistence."""
    result = _to_plain(config)
    if not isinstance(result, dict):  # pragma: no cover - defensive
        raise TypeError("configuration root must serialize to a mapping")
    return result


def to_public_dict(config: BaseModel) -> dict[str, Any]:
    """Plain dict with configured secrets replaced by a fixed mask."""
    public = copy.deepcopy(to_raw_dict(config))
    for path in iter_secret_paths(config):
        if _get_path(public, path):
            _set_path(public, path, MASK)
    return public
