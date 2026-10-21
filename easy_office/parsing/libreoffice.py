"""Headless LibreOffice adapter for legacy ``.doc`` / ``.ppt`` pagination.

LibreOffice is an external runtime, not a Python dependency. We discover it
from configuration, an environment variable, ``PATH`` and the usual install
locations; when it is missing the caller receives an actionable error instead
of a silent fallback, because silently skipping the conversion would lose the
whole document.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LEGACY_EXTENSIONS = {"doc", "ppt"}


class LibreOfficeError(RuntimeError):
    """Raised when headless LibreOffice conversion fails."""


class LibreOfficeNotFound(LibreOfficeError):
    """Raised when no ``soffice`` executable can be located."""


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for base in (program_files, program_files_x86):
        if base:
            candidates.append(Path(base) / "LibreOffice" / "program" / "soffice.exe")
    for drive in ("C:", "D:", "E:"):
        candidates.append(Path(f"{drive}/Program Files/LibreOffice/program/soffice.exe"))
        candidates.append(Path(f"{drive}/LibreOffice/program/soffice.exe"))
    candidates.extend(
        [
            Path("/usr/bin/soffice"),
            Path("/usr/local/bin/soffice"),
            Path("/usr/lib/libreoffice/program/soffice"),
            Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        ]
    )
    return candidates


def discover_libreoffice(explicit: str = "") -> Path:
    """Locate the LibreOffice executable or raise :class:`LibreOfficeNotFound`."""
    if explicit and explicit.strip():
        path = Path(os.path.expandvars(explicit.strip())).expanduser()
        if path.is_file():
            return path.resolve()
        raise LibreOfficeNotFound(
            f"local_parser.libreoffice_path points to a missing file: {path}"
        )
    env_value = os.environ.get("EASYOFFICE_SOFFICE", "").strip()
    if env_value:
        path = Path(env_value).expanduser()
        if path.is_file():
            return path.resolve()
    for name in ("soffice", "soffice.exe", "libreoffice"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    for candidate in _candidate_paths():
        if candidate.is_file():
            return candidate.resolve()
    raise LibreOfficeNotFound(
        "LibreOffice (soffice) was not found. Install it from "
        "https://www.libreoffice.org/download/ and either add it to PATH, set "
        "the EASYOFFICE_SOFFICE environment variable, or configure "
        "local_parser.libreoffice_path in config.yaml."
    )


def _build_command(
    executable: Path,
    source: Path,
    output_dir: Path,
    profile_dir: Path,
) -> list[str]:
    return [
        str(executable),
        f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(source),
    ]


def _creation_flags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


class LibreOfficeConverter:
    """Synchronous and asynchronous headless conversion with isolated profiles."""

    def __init__(self, executable: Path, *, timeout_seconds: float = 180.0) -> None:
        self._executable = Path(executable)
        self._timeout = float(timeout_seconds)

    @property
    def executable(self) -> Path:
        return self._executable

    def convert_to_pdf(
        self,
        source: str | Path,
        *,
        output_dir: str | Path,
        timeout_seconds: float | None = None,
    ) -> Path:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise LibreOfficeError(f"source file does not exist: {source_path}")
        out_dir = Path(output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        expected = out_dir / f"{source_path.stem}.pdf"
        timeout = timeout_seconds or self._timeout
        last_error: Exception | None = None
        # LibreOffice occasionally crashes on the very first headless start on
        # Windows (0xC0000409) while the detached converter still succeeds.
        # Retry once and accept an output file produced despite a bad rc.
        for attempt in range(2):
            # Keep the LibreOffice user profile on a short system-temp path.
            # Deep workspace paths (pytest temp trees, managed archives) can
            # exceed the effective MAX_PATH used by LibreOffice on Windows and
            # make the headless launcher crash with 0xC0000409.
            profile_root = Path(tempfile.mkdtemp(prefix="eo_lo_"))
            profile = profile_root / "profile"
            profile.mkdir(parents=True, exist_ok=True)
            command = _build_command(self._executable, source_path, out_dir, profile)
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    creationflags=_creation_flags(),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                last_error = LibreOfficeError(
                    f"LibreOffice conversion timed out after {timeout}s: "
                    f"{source_path.name}"
                )
                if expected.is_file():
                    return expected
                continue
            finally:
                shutil.rmtree(profile_root, ignore_errors=True)
            if completed.returncode == 0 or expected.is_file():
                if not expected.is_file():
                    last_error = LibreOfficeError(
                        f"LibreOffice reported success but no PDF was produced for: "
                        f"{source_path.name}"
                    )
                    continue
                return expected
            last_error = LibreOfficeError(
                f"LibreOffice conversion failed ({completed.returncode}): "
                f"{(completed.stderr or completed.stdout or '').strip()[:800]}"
            )
        assert last_error is not None
        raise last_error

    async def convert_to_pdf_async(
        self,
        source: str | Path,
        *,
        output_dir: str | Path,
        timeout_seconds: float | None = None,
    ) -> Path:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise LibreOfficeError(f"source file does not exist: {source_path}")
        out_dir = Path(output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        profile_root = Path(tempfile.mkdtemp(prefix="eo_lo_"))
        profile = profile_root / "profile"
        profile.mkdir(parents=True, exist_ok=True)
        expected = out_dir / f"{source_path.stem}.pdf"
        command = _build_command(self._executable, source_path, out_dir, profile)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_creation_flags(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_seconds or self._timeout
                )
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.wait()
                raise LibreOfficeError(
                    f"LibreOffice conversion timed out after {timeout_seconds or self._timeout}s: "
                    f"{source_path.name}"
                ) from exc
        finally:
            shutil.rmtree(profile_root, ignore_errors=True)
        if process.returncode != 0 and expected.is_file():
            # Same launcher quirk as the synchronous path: the document was
            # converted even though the launcher returned an abnormal code.
            return expected
        if process.returncode != 0:
            message = (stderr or stdout or b"").decode("utf-8", errors="replace")
            raise LibreOfficeError(
                f"LibreOffice conversion failed ({process.returncode}): {message.strip()[:800]}"
            )
        if not expected.is_file():
            raise LibreOfficeError(
                f"LibreOffice reported success but no PDF was produced for: {source_path.name}"
            )
        return expected
