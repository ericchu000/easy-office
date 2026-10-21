"""Asset cache for cropped document images and temporary render workspaces."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

_SAFE_UID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _safe_uid(file_uid: str) -> str:
    if not file_uid or any(ch not in _SAFE_UID_CHARS for ch in file_uid):
        raise ValueError(f"unsafe file UID for asset cache: {file_uid!r}")
    return file_uid


class AssetCache:
    """Stores ``<root>/<uid[:2]>/<uid>/<kind>-p<page>-<n>.<ext>`` and temp files."""

    def __init__(self, root: str | Path, *, temp_root: str | Path | None = None) -> None:
        self._root = Path(root)
        self._temp_root = Path(temp_root) if temp_root is not None else self._root / "_tmp"

    @property
    def root(self) -> Path:
        return self._root

    @property
    def temp_root(self) -> Path:
        return self._temp_root

    def reconfigure(
        self,
        *,
        root: str | Path | None = None,
        temp_root: str | Path | None = None,
    ) -> None:
        if root is not None:
            self._root = Path(root)
        if temp_root is not None:
            self._temp_root = Path(temp_root)

    def directory_for(self, file_uid: str) -> Path:
        uid = _safe_uid(file_uid)
        return self._root / uid[:2] / uid

    def path_for(
        self,
        file_uid: str,
        kind: str,
        *,
        page_no: int | None,
        index: int,
        extension: str = "png",
    ) -> Path:
        suffix = f"p{page_no}" if page_no is not None else "px"
        safe_kind = "".join(ch for ch in kind if ch.isalnum() or ch in "-_") or "asset"
        ext = extension.lstrip(".").lower() or "png"
        return self.directory_for(file_uid) / f"{safe_kind}-{suffix}-{index}.{ext}"

    def write_bytes(self, file_uid: str, kind: str, data: bytes, **kwargs) -> Path:
        path = self.path_for(file_uid, kind, **kwargs)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
        return path

    def temp_workspace(self, file_uid: str) -> Path:
        """A per-file scratch directory; callers remove it when done."""
        uid = _safe_uid(file_uid)
        path = self._temp_root / f"{uid}-{uuid.uuid4().hex[:12]}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def clear(self, file_uid: str) -> bool:
        directory = self.directory_for(file_uid)
        if not directory.exists():
            return False
        shutil.rmtree(directory, ignore_errors=True)
        try:
            directory.parent.rmdir()
        except OSError:
            pass
        return True

    @staticmethod
    def remove_workspace(path: str | Path | None) -> None:
        if path is None:
            return
        shutil.rmtree(Path(path), ignore_errors=True)
