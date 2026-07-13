"""File I/O RPCs — sandboxed preview + FE-uploaded attachment save.

Extracted from :mod:`ember_code.backend.server`. Two free
functions taking ``BackendServer`` as arg:

* :func:`read_file` — read a small text file for the FE preview
  card. Sandboxed to the project dir + ``~/.ember`` so the
  RPC can't be abused as a general file API.
* :func:`upload_attachment` — persist a FE-uploaded file
  (OS picker / drag / paste) to a per-session attachments dir
  so the agent's Read tool can pick it up on demand.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import base64
import re
from os.path import expanduser
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ember_code.backend.server_helpers import _guess_language, _is_within

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer


class ReadFileResult(BaseModel):
    """Wire shape for :func:`read_file` — sandboxed FE preview.

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
    """Wire shape for :func:`upload_attachment` — persisted FE-
    uploaded file location + byte count. ``error`` is empty on
    success."""

    path: str
    size: int
    error: str = ""


# Cap on ``read_file`` size. The RPC is only invoked by the
# plain-browser fallback preview — Tauri / VSCode / JetBrains
# hosts always go through their native open bridge and never
# hit this path. So this isn't a policy on what's openable,
# it's a guard for the in-app preview which isn't meant to be
# an editor for large files.
_READ_FILE_MAX_BYTES = 256 * 1024

# Substitution used to sanitise uploaded filenames before we
# write them to disk. Everything outside [A-Za-z0-9._-] becomes
# ``_`` so the FE can't traverse out of the attachments dir
# with a hostile filename.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def read_file(backend: "BackendServer", path: str) -> ReadFileResult:
    """Read a small text file for FE preview.

    Sandboxed: the resolved path must live under the current
    project dir OR under ``~/.ember`` (covers global hooks,
    settings, plugin sources). Anywhere else returns an error
    rather than reading — this is for read-only UI previews,
    not a general file API.
    """
    try:
        requested = Path(path).expanduser()
        if not requested.is_absolute():
            requested = (backend._session.project_dir / requested).resolve()
        else:
            requested = requested.resolve()
    except Exception as exc:
        return ReadFileResult(path=path, contents="", size=0, error=f"bad path: {exc}")

    project_root = Path(backend._session.project_dir).resolve()
    ember_root = Path(expanduser("~/.ember")).resolve()
    if not (_is_within(requested, project_root) or _is_within(requested, ember_root)):
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
        return ReadFileResult(
            path=str(requested), contents="", size=0, error="File not found."
        )
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
            error=f"File too large to preview ({size} bytes; cap {_READ_FILE_MAX_BYTES}).",
        )
    try:
        contents = requested.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError) as exc:
        return ReadFileResult(
            path=str(requested), contents="", size=size, error=f"read failed: {exc}"
        )
    return ReadFileResult(
        path=str(requested),
        contents=contents,
        size=size,
        language=_guess_language(requested.suffix),
    )


def upload_attachment(
    backend: "BackendServer",
    filename: str,
    content_base64: str,
) -> UploadAttachmentResult:
    """Persist a FE-uploaded file (OS picker / drag / paste) to a
    per-session attachments dir so the agent can read it on
    demand via its Read tool.

    Content is base64 so the FE can ship arbitrary bytes (PDFs,
    images) over the JSON wire.
    """
    # Strip path separators / nasty chars so the FE can't write
    # outside the attachments dir.
    safe = _SAFE_NAME_RE.sub("_", filename) or "file"
    dest_dir = backend._session.project_dir / ".ember" / "attachments" / backend._session.session_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe
    # If a same-name file already exists, suffix to avoid
    # overwrite.
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
    except Exception as exc:
        return UploadAttachmentResult(path="", size=0, error=f"invalid base64: {exc}")
    dest.write_bytes(data)
    return UploadAttachmentResult(path=str(dest), size=len(data))
