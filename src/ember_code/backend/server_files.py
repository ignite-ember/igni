"""File I/O RPCs — sandboxed preview + FE-uploaded attachment save.

:class:`FilesController` is the sole public surface, constructed with
a :class:`Session`.

* :meth:`FilesController.read_file` — sandboxed text file
  preview for the FE.
* :meth:`FilesController.upload_attachment` — persist a
  FE-uploaded file to a per-session attachments dir.
"""

from __future__ import annotations

import base64
import binascii
import re
from os.path import expanduser
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ember_code.core.session import Session


class ReadFileResult(BaseModel):
    """Wire shape for :meth:`FilesController.read_file` — sandboxed
    FE preview.

    ``error`` is empty on success; ``language`` is the detected
    highlight hint (extension → name mapping) and stays empty on
    every non-success path so the FE never mis-highlights an
    error payload."""

    path: str
    contents: str
    size: int
    error: str = ""
    language: str = ""


class UploadAttachmentResult(BaseModel):
    """Wire shape for :meth:`FilesController.upload_attachment` —
    persisted FE-uploaded file location + byte count."""

    path: str
    size: int
    error: str = ""


# Cap on ``read_file`` size. The RPC is only invoked by the
# plain-browser fallback preview.
_READ_FILE_MAX_BYTES = 256 * 1024

# Substitution used to sanitise uploaded filenames before we
# write them to disk.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class FilesController:
    """File-I/O RPCs for one :class:`Session`.

    Owns the sandbox-check and language-guess helpers as private
    methods — both are used only from this class, so a module-level
    free-function seam would be pure ceremony.
    """

    # Filename-extension → Prism-compatible highlight hint. Kept on
    # the controller class (not module scope) so behavior + data sit
    # together — a second consumer would trigger a promotion to a
    # dedicated LanguageHints class.
    _LANG_BY_EXT: dict[str, str] = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".json": "json",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".md": "markdown",
        ".markdown": "markdown",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".html": "html",
        ".css": "css",
        ".sql": "sql",
    }

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Sandbox / language helpers ─────────────────────────────────

    @staticmethod
    def _within_root(child: Path, root: Path) -> bool:
        """True iff ``child`` (already resolved) sits under ``root``."""
        try:
            child.relative_to(root)
            return True
        except ValueError:
            return False

    @classmethod
    def _guess_language(cls, suffix: str) -> str:
        """Return the Prism-compatible language string for a file
        suffix. Unknown extensions collapse to ``""`` so the FE
        renders the file as plain text."""
        return cls._LANG_BY_EXT.get(suffix.lower(), "")

    def read_file(self, path: str) -> ReadFileResult:
        """Read a small text file for FE preview.

        Sandboxed: the resolved path must live under the current
        project dir OR under ``~/.ember``.
        """
        try:
            requested = Path(path).expanduser()
            if not requested.is_absolute():
                requested = (self._session.project_dir / requested).resolve()
            else:
                requested = requested.resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            # Narrow catch: Path.resolve raises OSError, expanduser can
            # raise RuntimeError when HOME is unresolvable, and malformed
            # inputs surface as ValueError. Anything else should bubble.
            return ReadFileResult(path=path, contents="", size=0, error=f"bad path: {exc}")

        project_root = Path(self._session.project_dir).resolve()
        ember_root = Path(expanduser("~/.ember")).resolve()
        if not (
            self._within_root(requested, project_root) or self._within_root(requested, ember_root)
        ):
            return ReadFileResult(
                path=str(requested),
                contents="",
                size=0,
                error=(
                    "Refused: path is outside the project and ~/.ember. "
                    "Open it in your editor instead."
                ),
            )
        if not requested.exists():
            return ReadFileResult(path=str(requested), contents="", size=0, error="File not found.")
        if requested.is_dir():
            return ReadFileResult(
                path=str(requested), contents="", size=0, error="Path is a directory."
            )

        size = requested.stat().st_size
        if size > _READ_FILE_MAX_BYTES:
            return ReadFileResult(
                path=str(requested),
                contents="",
                size=size,
                error=(f"File too large to preview ({size} bytes; cap {_READ_FILE_MAX_BYTES})."),
            )
        try:
            contents = requested.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError) as exc:
            return ReadFileResult(
                path=str(requested),
                contents="",
                size=size,
                error=f"read failed: {exc}",
            )
        return ReadFileResult(
            path=str(requested),
            contents=contents,
            size=size,
            language=self._guess_language(requested.suffix),
        )

    def upload_attachment(self, filename: str, content_base64: str) -> UploadAttachmentResult:
        """Persist a FE-uploaded file to a per-session attachments
        dir."""
        safe = _SAFE_NAME_RE.sub("_", filename) or "file"
        dest_dir = self._session.project_dir / ".ember" / "attachments" / self._session.session_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe
        if dest.exists():
            stem, dot, ext = safe.rpartition(".")
            base = stem if dot else safe
            suffix = ext if dot else ""
            n = 2
            while dest.exists():
                dest = dest_dir / (f"{base}-{n}{('.' + suffix) if suffix else ''}")
                n += 1
        try:
            data = base64.b64decode(content_base64)
        except (binascii.Error, ValueError) as exc:
            return UploadAttachmentResult(path="", size=0, error=f"invalid base64: {exc}")
        dest.write_bytes(data)
        return UploadAttachmentResult(path=str(dest), size=len(data))
