"""File upload handler — DELIBERATELY VULNERABLE for the eval.

Accepts a relative ``filename`` and writes the body straight to disk
without sanitizing path-traversal sequences (``../``). A caller can
escape the upload directory and overwrite anything the process can
reach. Tagged ``vulnerabilities=[path-traversal]``,
``security=critical``, ``domain=[web, uploads]``.
"""

from __future__ import annotations

from pathlib import Path

UPLOAD_DIR = Path("/tmp/uploads")


def save_upload(filename: str, body: bytes) -> str:
    """Write ``body`` to ``UPLOAD_DIR/filename``.

    Bug on purpose: ``filename`` is joined onto ``UPLOAD_DIR`` without
    any normalization or containment check. ``../../etc/passwd`` works.
    """
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / filename
    target.write_bytes(body)
    return str(target)


def read_upload(filename: str) -> bytes:
    """Reflect the same bug on the read side — symmetric escape."""
    target = UPLOAD_DIR / filename
    return target.read_bytes()
