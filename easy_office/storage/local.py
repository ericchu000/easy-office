"""Local filesystem storage driver for both physical storage modes."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable

from easy_office.storage.base import (
    FileStorage,
    PathNotAllowedError,
    StorageConflictError,
    StorageError,
    StorageMode,
    StorageNotFoundError,
    StoredObject,
)
from easy_office.utils import coerce_datetime, new_uid, utc_now

_CHUNK_SIZE = 1024 * 1024
_URI_PREFIX = "local:"
_UID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ILLEGAL_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_MIME_BY_EXTENSION = {
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "txt": "text/plain",
    "md": "text/markdown",
    "json": "application/json",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "heic": "image/heic",
}


def detect_mime_type(file_name: str) -> str:
    """Detect MIME type by extension with a deterministic office-document map.

    Windows' registry-backed ``mimetypes`` database can vary between machines,
    so common office formats are pinned explicitly before falling back to it.
    """
    extension = Path(file_name).suffix.lower().lstrip(".")
    if extension in _MIME_BY_EXTENSION:
        return _MIME_BY_EXTENSION[extension]
    guessed, _ = mimetypes.guess_type(file_name, strict=False)
    return guessed or "application/octet-stream"


def file_extension(file_name: str) -> str:
    suffix = Path(file_name).suffix
    return suffix[1:].lower() if suffix else ""


def safe_filename(file_name: str) -> str:
    """Reduce a client-provided name to a safe, single-segment file name."""
    normalized = unicodedata.normalize("NFC", str(file_name))
    # Accept both separators so a malicious "../../x" cannot escape the workspace.
    normalized = normalized.replace("\\", "/").split("/")[-1]
    normalized = _ILLEGAL_FILENAME.sub("_", normalized).strip().rstrip(".")
    if not normalized or normalized in {".", ".."}:
        normalized = "file"
    stem, suffix = os.path.splitext(normalized)
    if stem.upper() in _WINDOWS_RESERVED:
        normalized = f"_{normalized}"
        stem, suffix = os.path.splitext(normalized)
    if len(normalized) > 180:
        normalized = stem[: max(1, 180 - len(suffix))] + suffix
    return normalized


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve(path: str | Path, *, must_exist: bool) -> Path:
    candidate = Path(os.path.expandvars(os.fspath(path))).expanduser()
    try:
        return candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        if must_exist:
            raise StorageNotFoundError(f"path does not exist: {candidate}") from exc
        raise StorageError(f"cannot resolve path: {candidate}") from exc


class LocalFileStorage(FileStorage):
    """Filesystem implementation of :class:`FileStorage`.

    Managed uploads live under ``managed_workspace``; host in-place references
    are accepted only inside ``allowed_roots`` when that list is non-empty.
    """

    def __init__(
        self,
        managed_workspace: str | Path,
        allowed_roots: Iterable[str | Path] = (),
        *,
        max_upload_bytes: int = 4 * 1024 * 1024 * 1024,
    ) -> None:
        self._workspace = _resolve(managed_workspace, must_exist=False)
        self._allowed_roots = tuple(_resolve(item, must_exist=False) for item in allowed_roots)
        self._max_upload_bytes = int(max_upload_bytes)
        if self._max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")

    # ------------------------------------------------------------- properties
    @property
    def managed_workspace(self) -> Path:
        return self._workspace

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return self._allowed_roots

    def reconfigure(
        self,
        *,
        managed_workspace: str | Path | None = None,
        allowed_roots: Iterable[str | Path] | None = None,
        max_upload_bytes: int | None = None,
    ) -> None:
        if managed_workspace is not None:
            self._workspace = _resolve(managed_workspace, must_exist=False)
        if allowed_roots is not None:
            self._allowed_roots = tuple(
                _resolve(item, must_exist=False) for item in allowed_roots
            )
        if max_upload_bytes is not None:
            if int(max_upload_bytes) <= 0:
                raise ValueError("max_upload_bytes must be positive")
            self._max_upload_bytes = int(max_upload_bytes)

    # ------------------------------------------------------------------- URIs
    @staticmethod
    def make_uri(path: Path) -> str:
        return f"{_URI_PREFIX}{path}"

    @staticmethod
    def path_from_uri(uri: str) -> Path:
        if not isinstance(uri, str) or not uri.startswith(_URI_PREFIX):
            raise StorageError(f"unsupported object URI: {uri!r}")
        return Path(uri[len(_URI_PREFIX) :])

    def infer_mode(self, uri: str) -> StorageMode:
        path = self.path_from_uri(uri).resolve(strict=False)
        return StorageMode.MANAGED if _is_within(path, self._workspace) else StorageMode.INPLACE

    # ------------------------------------------------------------- validation
    def _check_inplace_allowed(self, path: Path) -> None:
        if not self._allowed_roots:
            return
        if not any(_is_within(path, root) for root in self._allowed_roots):
            roots = ", ".join(str(root) for root in self._allowed_roots)
            raise PathNotAllowedError(
                f"path is outside the configured host allow-list: {path} (allowed: {roots})"
            )

    def _check_managed_path(self, path: Path) -> None:
        if not _is_within(path, self._workspace):
            raise PathNotAllowedError(
                f"path is outside the managed workspace: {path} (workspace: {self._workspace})"
            )

    # -------------------------------------------------------- in-place objects
    def describe_inplace(self, path: str | Path, *, file_uid: str | None = None) -> StoredObject:
        return self._describe(path, StorageMode.INPLACE, file_uid=file_uid)

    def describe_existing(
        self,
        path: str | Path,
        storage_mode: StorageMode | str,
        *,
        file_uid: str | None = None,
    ) -> StoredObject:
        """Describe a file that is already stored in either storage mode."""
        return self._describe(path, StorageMode(storage_mode), file_uid=file_uid)

    def _describe(
        self,
        path: str | Path,
        storage_mode: StorageMode,
        *,
        file_uid: str | None,
    ) -> StoredObject:
        if file_uid is not None and not _UID_PATTERN.fullmatch(file_uid):
            raise StorageError(f"invalid file UID: {file_uid!r}")
        resolved = _resolve(path, must_exist=True)
        if not resolved.is_file():
            raise StorageError(f"not a regular file: {resolved}")
        if storage_mode == StorageMode.INPLACE:
            self._check_inplace_allowed(resolved)
            roots = self._allowed_roots
        else:
            self._check_managed_path(resolved)
            roots = (self._workspace,)

        stat = resolved.stat()
        created_ts = getattr(stat, "st_birthtime", None) or stat.st_ctime
        created = datetime.fromtimestamp(created_ts, tz=timezone.utc)
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        relative: str | None = None
        for root in roots:
            if _is_within(resolved, root):
                relative = str(resolved.relative_to(root))
                break

        return StoredObject(
            file_uid=file_uid or new_uid(),
            uri=self.make_uri(resolved),
            local_path=resolved,
            storage_mode=storage_mode,
            file_name=resolved.name,
            extension=file_extension(resolved.name),
            mime_type=detect_mime_type(resolved.name),
            size_bytes=int(stat.st_size),
            parent_dir=str(resolved.parent),
            content_hash=self.compute_sha256(self.make_uri(resolved)),
            fs_created_at=created,
            fs_modified_at=modified,
            relative_path=relative,
        )

    def register_inplace(self, path: str | Path) -> StoredObject:
        return self.describe_inplace(path)

    # ------------------------------------------------------- managed uploads
    def ingest_stream(
        self,
        file_name: str,
        stream: BinaryIO,
        *,
        fs_created_at: datetime | str | int | float | None = None,
        fs_modified_at: datetime | str | int | float | None = None,
        file_uid: str | None = None,
        relative_path: str | None = None,
    ) -> StoredObject:
        clean_name = safe_filename(file_name)
        uid = file_uid or new_uid()
        if not _UID_PATTERN.fullmatch(uid):
            raise StorageError(f"invalid file UID: {uid!r}")
        self._workspace.mkdir(parents=True, exist_ok=True)
        destination = self._workspace / uid[:2] / uid / clean_name
        if destination.exists():
            raise StorageConflictError(f"managed object already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)

        created = coerce_datetime(fs_created_at, default=utc_now(), field="fs_created_at")
        if fs_modified_at is None:
            modified = created
        else:
            modified = coerce_datetime(fs_modified_at, field="fs_modified_at")

        digest = hashlib.sha256()
        size = 0
        try:
            with open(destination, "xb") as handle:
                while True:
                    chunk = stream.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        raise StorageError("upload stream must be opened in binary mode")
                    size += len(chunk)
                    if size > self._max_upload_bytes:
                        raise StorageError(
                            f"upload exceeds the configured limit of {self._max_upload_bytes} bytes"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            self._prune_empty_dirs(destination.parent)
            raise

        return StoredObject(
            file_uid=uid,
            uri=self.make_uri(destination),
            local_path=destination,
            storage_mode=StorageMode.MANAGED,
            file_name=clean_name,
            extension=file_extension(clean_name),
            mime_type=detect_mime_type(clean_name),
            size_bytes=size,
            parent_dir=str(destination.parent),
            content_hash=digest.hexdigest(),
            fs_created_at=created,
            fs_modified_at=modified,
            relative_path=relative_path,
        )

    # ------------------------------------------------------------ read access
    def open_stream(self, uri: str) -> BinaryIO:
        path = self.path_from_uri(uri)
        if not path.is_file():
            raise StorageNotFoundError(f"object not found: {path}")
        return open(path, "rb")

    def locate(self, uri: str) -> Path:
        path = self.path_from_uri(uri)
        if not path.exists():
            raise StorageNotFoundError(f"object not found: {path}")
        return path

    def compute_sha256(self, uri: str) -> str:
        path = self.path_from_uri(uri)
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError as exc:
            raise StorageError(f"cannot hash object: {path}: {exc}") from exc
        return digest.hexdigest()

    def exists(self, uri: str) -> bool:
        try:
            return self.path_from_uri(uri).is_file()
        except StorageError:
            return False

    # ----------------------------------------------------------- manipulation
    def move(
        self,
        src_uri: str,
        dst_uri: str,
        *,
        mode: StorageMode | str | None = None,
        overwrite: bool = False,
    ) -> str:
        source = self.path_from_uri(src_uri).resolve(strict=False)
        destination = self.path_from_uri(dst_uri).resolve(strict=False)
        if not source.is_file():
            raise StorageNotFoundError(f"source object not found: {source}")
        storage_mode = StorageMode(mode) if mode is not None else self.infer_mode(src_uri)
        if storage_mode == StorageMode.MANAGED:
            self._check_managed_path(source)
            self._check_managed_path(destination)
        else:
            self._check_inplace_allowed(source)
            self._check_inplace_allowed(destination)
        if source == destination:
            return dst_uri
        if destination.is_dir():
            raise StorageError("move destination must be a file path, not a directory")
        if destination.exists() and not overwrite:
            raise StorageConflictError(f"destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        if storage_mode == StorageMode.MANAGED:
            self._prune_empty_dirs(source.parent)
        return self.make_uri(destination)

    def copy(
        self,
        src_uri: str,
        dst_uri: str,
        *,
        mode: StorageMode | str | None = None,
        overwrite: bool = False,
    ) -> str:
        source = self.path_from_uri(src_uri).resolve(strict=False)
        destination = self.path_from_uri(dst_uri).resolve(strict=False)
        if not source.is_file():
            raise StorageNotFoundError(f"source object not found: {source}")
        storage_mode = StorageMode(mode) if mode is not None else self.infer_mode(src_uri)
        if storage_mode == StorageMode.MANAGED:
            self._check_managed_path(source)
            self._check_managed_path(destination)
        else:
            self._check_inplace_allowed(source)
            self._check_inplace_allowed(destination)
        if source == destination:
            return dst_uri
        if destination.is_dir():
            raise StorageError("copy destination must be a file path, not a directory")
        if destination.exists() and not overwrite:
            raise StorageConflictError(f"destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(destination))
        return self.make_uri(destination)

    def delete(self, uri: str) -> bool:
        path = self.path_from_uri(uri).resolve(strict=False)
        self._check_managed_path(path)
        if not path.is_file():
            return False
        path.unlink()
        self._prune_empty_dirs(path.parent)
        return True

    # ----------------------------------------------------------------- helpers
    def _prune_empty_dirs(self, start: Path) -> None:
        current = start
        workspace = self._workspace.resolve(strict=False)
        while _is_within(current, workspace) and current != workspace:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
