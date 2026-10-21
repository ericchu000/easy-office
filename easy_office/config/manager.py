"""Thread-safe configuration manager with atomic persistence and hot reload."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml
from pydantic import BaseModel, SecretStr, ValidationError

from easy_office.config.models import (
    MASK,
    AppConfig,
    ServiceMode,
    to_public_dict,
    to_raw_dict,
)
from easy_office.utils import atomic_write_text

logger = logging.getLogger(__name__)

Listener = Callable[[AppConfig], None]
Validator = Callable[[AppConfig, AppConfig], None]


class ConfigError(Exception):
    """Base class for configuration failures."""


class ConfigValidationError(ConfigError):
    """Raised when a configuration file or update fails validation."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        self.errors = errors or []
        detail = f"{message}: {self.errors}" if self.errors else message
        super().__init__(detail)


@dataclass(frozen=True)
class ResolvedPaths:
    """Absolute paths derived from the current configuration.

    ``config_root`` is the project/config directory (config, models, managed
    source files, user watch roots); ``data_dir`` is the rebuildable runtime
    root (database, mirror, assets, temp).
    """

    data_dir: Path
    config_path: Path
    managed_workspace: Path
    index_db_path: Path
    mirror_dir: Path
    asset_cache_dir: Path
    temp_dir: Path
    weights_dir: Path
    host_watch_dirs: tuple[Path, ...]
    host_allowed_roots: tuple[Path, ...]


@dataclass(frozen=True)
class ConnectivityResult:
    target: str
    status: str
    message: str
    latency_ms: float | None = None
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_config_path(project_root: Path) -> Path:
    env_config = os.environ.get("EASYOFFICE_CONFIG")
    if env_config:
        return Path(os.path.expandvars(env_config)).expanduser().resolve(strict=False)
    return project_root / "config.yaml"


def _default_runtime_dir(project_root: Path) -> Path:
    env_runtime = os.environ.get("EASYOFFICE_RUNTIME") or os.environ.get("EASYOFFICE_HOME")
    if env_runtime:
        return Path(os.path.expandvars(env_runtime)).expanduser().resolve(strict=False)
    return project_root / "runtime"


def _format_validation_error(exc: ValidationError) -> list[str]:
    formatted: list[str] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        formatted.append(f"{location or '<root>'}: {item.get('msg', 'invalid value')}")
    return formatted


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(value, Mapping) and isinstance(existing, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _current_at(current: Any, key: Any) -> Any:
    if isinstance(current, BaseModel):
        return getattr(current, key, _MISSING)
    if isinstance(current, Mapping):
        return current.get(key, _MISSING)
    return _MISSING


def _strip_masked(patch: Mapping[str, Any], current: Any) -> dict[str, Any]:
    """Remove mask placeholders so a public config can be saved back safely."""
    cleaned: dict[str, Any] = {}
    for key, value in patch.items():
        existing = _current_at(current, key)
        if isinstance(value, Mapping) and existing is not _MISSING:
            nested = _strip_masked(value, existing)
            if nested:
                cleaned[str(key)] = nested
            continue
        if isinstance(existing, SecretStr) and value == MASK:
            continue
        cleaned[str(key)] = copy.deepcopy(value)
    return cleaned


def _build_patch(path: str, value: Any) -> dict[str, Any]:
    parts = [part for part in path.split(".") if part]
    if not parts:
        raise ValueError("configuration path must not be empty")
    patch: Any = value
    for part in reversed(parts):
        patch = {part: patch}
    return patch


_MISSING = object()


class ConfigManager:
    """Owns the live configuration and notifies subscribers on every change.

    Updates are validated, persisted atomically to YAML, then applied. Listeners
    are notified after the swap so a failing consumer cannot block persistence.
    """

    def __init__(
        self,
        config_path: str | Path | None = None,
        data_dir: str | Path | None = None,
        *,
        project_root: str | Path | None = None,
        create_if_missing: bool = True,
    ) -> None:
        root = (
            Path(os.path.expandvars(os.fspath(project_root))).expanduser().resolve(strict=False)
            if project_root is not None
            else _default_project_root()
        )
        if config_path is None:
            resolved_config = _default_config_path(root)
        else:
            resolved_config = Path(
                os.path.expandvars(os.fspath(config_path))
            ).expanduser().resolve(strict=False)

        resolved_data = (
            Path(os.path.expandvars(os.fspath(data_dir))).expanduser().resolve(strict=False)
            if data_dir is not None
            else _default_runtime_dir(root)
        )

        self._lock = threading.RLock()
        self._project_root = root
        self._data_dir = resolved_data
        self._config_path = resolved_config
        self._listeners: list[Listener] = []
        self._validators: list[Validator] = []
        self._create_if_missing = create_if_missing

        if create_if_missing:
            self._data_dir.mkdir(parents=True, exist_ok=True)

        self._config = self._load_initial()

    # ------------------------------------------------------------------ paths
    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def project_root(self) -> Path:
        return self._project_root

    def resolve_runtime_path(self, value: str | Path) -> Path:
        """Resolve a path relative to the rebuildable runtime directory."""
        text = os.path.expandvars(os.fspath(value))
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self._data_dir / candidate
        return candidate.resolve(strict=False)

    def resolve_project_path(self, value: str | Path) -> Path:
        """Resolve a path relative to the configuration/project directory."""
        text = os.path.expandvars(os.fspath(value))
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self._config_path.parent / candidate
        return candidate.resolve(strict=False)

    def resolve_path(self, value: str | Path) -> Path:
        """Backward-compatible alias for runtime-relative resolution."""
        return self.resolve_runtime_path(value)

    def paths(self) -> ResolvedPaths:
        with self._lock:
            config = self._config
        return ResolvedPaths(
            data_dir=self._data_dir,
            config_path=self._config_path,
            managed_workspace=self.resolve_project_path(config.storage.managed_workspace),
            index_db_path=self.resolve_runtime_path(config.storage.index_db_path),
            mirror_dir=self.resolve_runtime_path(config.storage.mirror_cache_dir),
            asset_cache_dir=self.resolve_runtime_path(config.storage.asset_cache_dir),
            temp_dir=self.resolve_runtime_path(config.storage.temp_dir),
            weights_dir=self.resolve_project_path(config.local_parser.weights_dir),
            host_watch_dirs=tuple(
                self.resolve_project_path(item) for item in config.storage.host_watch_dirs
            ),
            host_allowed_roots=tuple(
                self.resolve_project_path(item) for item in config.storage.host_allowed_roots
            ),
        )

    def ensure_directories(self) -> ResolvedPaths:
        paths = self.paths()
        paths.managed_workspace.mkdir(parents=True, exist_ok=True)
        paths.index_db_path.parent.mkdir(parents=True, exist_ok=True)
        paths.mirror_dir.mkdir(parents=True, exist_ok=True)
        paths.asset_cache_dir.mkdir(parents=True, exist_ok=True)
        paths.temp_dir.mkdir(parents=True, exist_ok=True)
        paths.weights_dir.mkdir(parents=True, exist_ok=True)
        return paths

    # --------------------------------------------------------------- accessors
    def get(self) -> AppConfig:
        with self._lock:
            return self._config.model_copy(deep=True)

    def public_config(self) -> dict[str, Any]:
        with self._lock:
            return to_public_dict(self._config)

    def raw_config(self) -> dict[str, Any]:
        """Full configuration including real secret values; never expose via APIs."""
        with self._lock:
            return to_raw_dict(self._config)

    # ---------------------------------------------------------------- mutation
    def update(
        self,
        patch: Mapping[str, Any],
        *,
        persist: bool = True,
    ) -> AppConfig:
        if not isinstance(patch, Mapping):
            raise TypeError("configuration patch must be a mapping")
        with self._lock:
            old = self._config
            cleaned_patch = _strip_masked(patch, old)
            if not cleaned_patch:
                return old.model_copy(deep=True)
            merged = _deep_merge(to_raw_dict(old), cleaned_patch)
            try:
                new = AppConfig.model_validate(merged)
            except ValidationError as exc:
                raise ConfigValidationError(
                    "configuration update rejected", _format_validation_error(exc)
                ) from exc
            self._validate_and_commit(old, new, persist=persist)
            return new.model_copy(deep=True)

    def set(self, path: str, value: Any, *, persist: bool = True) -> AppConfig:
        return self.update(_build_patch(path, value), persist=persist)

    def replace(self, config: AppConfig, *, persist: bool = True) -> AppConfig:
        if not isinstance(config, AppConfig):
            raise TypeError("replace() expects an AppConfig instance")
        with self._lock:
            old = self._config
            # Round-trip through raw data so masked placeholders are impossible here.
            new = AppConfig.model_validate(to_raw_dict(config))
            self._validate_and_commit(old, new, persist=persist)
            return new.model_copy(deep=True)

    def reset_to_defaults(self, *, persist: bool = True) -> AppConfig:
        return self.replace(AppConfig(), persist=persist)

    def _validate_and_commit(self, old: AppConfig, new: AppConfig, *, persist: bool) -> None:
        # Validators may raise any exception; the caller sees it unchanged and
        # neither the file nor the live configuration is mutated.
        for validator in list(self._validators):
            validator(old, new)

        if persist:
            self._persist(new)
        self._config = new

        listeners = list(self._listeners)
        if listeners:
            self._notify(listeners, new)

    def _notify(self, listeners: list[Listener], new: AppConfig) -> None:
        for listener in listeners:
            try:
                listener(new.model_copy(deep=True))
            except Exception:  # pragma: no cover - defensive isolation
                logger.exception("configuration listener failed")

    def _persist(self, config: AppConfig) -> None:
        payload = to_raw_dict(config)
        text = yaml.safe_dump(
            payload,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        atomic_write_text(self._config_path, text)

    # ------------------------------------------------------------- observers
    def subscribe(self, listener: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def add_validator(self, validator: Validator) -> Callable[[], None]:
        with self._lock:
            self._validators.append(validator)

        def remove() -> None:
            with self._lock:
                if validator in self._validators:
                    self._validators.remove(validator)

        return remove

    def reload(self) -> AppConfig:
        with self._lock:
            old = self._config
            new = self._read_config_file()
            for validator in list(self._validators):
                validator(old, new)
            self._config = new
            listeners = list(self._listeners)
        if listeners:
            self._notify(listeners, new)
        return new.model_copy(deep=True)

    # ---------------------------------------------------------------- loading
    def _load_initial(self) -> AppConfig:
        if self._config_path.exists():
            return self._read_config_file()
        if not self._create_if_missing:
            raise ConfigError(f"configuration file not found: {self._config_path}")
        config = AppConfig()
        self._persist(config)
        return config

    def _read_config_file(self) -> AppConfig:
        try:
            text = self._config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read configuration file: {exc}") from exc
        if not text.strip():
            return AppConfig()
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"configuration YAML is invalid: {exc}") from exc
        if raw is None:
            return AppConfig()
        if not isinstance(raw, Mapping):
            raise ConfigError("configuration root must be a mapping")
        try:
            return AppConfig.model_validate(dict(raw))
        except ValidationError as exc:
            raise ConfigValidationError(
                "configuration file failed validation", _format_validation_error(exc)
            ) from exc

    # ------------------------------------------------------------ connectivity
    def test_connectivity(self, target: str = "all") -> dict[str, Any]:
        """Probe configured model services without mutating configuration.

        Returns a mapping from component name to a structured result. Network
        failures are reported, never raised.
        """
        config = self.get()
        allowed = {"llm", "vlm", "embedding", "reranker", "local_parser", "all"}
        if target not in allowed:
            raise ValueError(f"unknown connectivity target: {target}")
        names = (
            ["llm", "vlm", "embedding", "reranker", "local_parser"]
            if target == "all"
            else [target]
        )
        results: dict[str, Any] = {}
        for name in names:
            if name == "local_parser":
                results[name] = self._probe_local_parser(config).to_dict()
            else:
                results[name] = self._probe_model(name, config).to_dict()
        return results

    @staticmethod
    def _weights_present(weights: Path) -> bool:
        """A directory alone is not proof of downloaded model weights."""
        if not weights.is_dir():
            return False
        try:
            return any(weights.iterdir())
        except OSError:
            return False

    def _probe_local_parser(self, config: AppConfig) -> ConnectivityResult:
        started = time.monotonic()
        weights = self.resolve_project_path(config.local_parser.weights_dir)
        present = self._weights_present(weights)
        elapsed = (time.monotonic() - started) * 1000.0
        detail = {
            "weights_dir": str(weights),
            "weights_present": present,
            "device": config.local_parser.device,
            "ocr_enabled": config.local_parser.ocr_enabled,
        }
        if present:
            return ConnectivityResult(
                "local_parser",
                "ok",
                f"weights found; device={config.local_parser.device}",
                round(elapsed, 2),
                detail,
            )
        return ConnectivityResult(
            "local_parser",
            "not_configured",
            f"no local model weights found under: {weights}",
            round(elapsed, 2),
            detail,
        )

    def _probe_model(self, name: str, config: AppConfig) -> ConnectivityResult:
        service = getattr(config, name)
        provider = service.provider
        if provider == ServiceMode.DISABLED or (name == "reranker" and not service.enabled):
            return ConnectivityResult(name, "disabled", "service is disabled")
        if provider == ServiceMode.LOCAL:
            weights = self.resolve_project_path(config.local_parser.weights_dir)
            present = self._weights_present(weights)
            status = "ok" if present else "not_configured"
            message = (
                "local provider is loaded in-process; model weights found"
                if present
                else "local provider requires model weights; weights are missing"
            )
            return ConnectivityResult(
                name,
                status,
                message,
                detail={"weights_dir": str(weights), "model": service.model_name},
            )
        if not service.base_url:
            return ConnectivityResult(
                name,
                "not_configured",
                "base_url is empty",
                detail={"provider": provider.value},
            )

        url = self._probe_url(provider, service.base_url)
        headers = {"Accept": "application/json"}
        api_key = service.api_key.get_secret_value() if isinstance(service.api_key, SecretStr) else ""
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        started = time.monotonic()
        try:
            request = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(request, timeout=5.0) as response:
                body = response.read(1024 * 1024).decode("utf-8", errors="replace")
                status_code = getattr(response, "status", 200)
            elapsed = (time.monotonic() - started) * 1000.0
            detail: dict[str, Any] = {"url": url, "http_status": status_code}
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    if isinstance(parsed.get("data"), list):
                        detail["model_count"] = len(parsed["data"])
                    if isinstance(parsed.get("models"), list):
                        detail["model_count"] = len(parsed["models"])
            except ValueError:
                pass
            return ConnectivityResult(
                name,
                "ok",
                "service responded successfully",
                round(elapsed, 2),
                detail,
            )
        except urllib.error.HTTPError as exc:
            elapsed = (time.monotonic() - started) * 1000.0
            return ConnectivityResult(
                name,
                "unreachable",
                f"HTTP {exc.code}: {exc.reason}",
                round(elapsed, 2),
                {"url": url, "http_status": exc.code},
            )
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            elapsed = (time.monotonic() - started) * 1000.0
            return ConnectivityResult(
                name,
                "unreachable",
                f"connection failed: {exc}",
                round(elapsed, 2),
                {"url": url},
            )

    @staticmethod
    def _probe_url(provider: ServiceMode, base_url: str) -> str:
        base = base_url.rstrip("/")
        if provider == ServiceMode.OLLAMA:
            return f"{base}/api/tags"
        if base.endswith("/models"):
            return base
        return f"{base}/models"
