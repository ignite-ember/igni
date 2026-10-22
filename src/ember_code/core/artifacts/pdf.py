"""HTML to PDF, and the library hunt that has to happen first.

WeasyPrint renders the same self-contained page the HTML export
writes, so the two formats cannot drift: one renderer, one stylesheet,
two containers.

Most of this module is not about PDF. It is about loading the C
libraries WeasyPrint binds to (pango, cairo, harfbuzz, fontconfig) on
a machine where they are installed somewhere the dynamic loader does
not look — which on macOS with Homebrew is every machine.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Where package managers put the shared libraries WeasyPrint needs.
#: Homebrew on Apple silicon, Homebrew on Intel, MacPorts.
_MAC_LIBRARY_DIRS: tuple[str, ...] = (
    "/opt/homebrew/lib",
    "/usr/local/lib",
    "/opt/local/lib",
)

#: Install line to put in front of somebody whose machine lacks them.
INSTALL_HINT = "brew install pango"


class PdfUnavailable(RuntimeError):
    """WeasyPrint is not importable, with a reason worth reading."""


class PdfRenderer:
    """Renders a rendered page into a PDF.

    The import is deferred to the first call rather than done at module
    scope: WeasyPrint pulls in its whole C stack on import, and a
    backend that cannot export PDF should still start.
    """

    _lock = threading.Lock()
    _html_class: Any = None
    _failure: str = ""

    @classmethod
    def _prepare_library_path(cls) -> None:
        """Point the loader at Homebrew before WeasyPrint looks.

        ``DYLD_FALLBACK_LIBRARY_PATH`` set here does *not* reach dyld —
        macOS reads that at process start, and strips it besides. What
        it does reach is :func:`ctypes.util.find_library`, which builds
        its candidate list from ``os.environ`` at call time, and which
        is what cffi falls back to when a bare ``dlopen`` misses.

        That distinction is the whole point of doing this in-process.
        Telling somebody to export the variable in their shell fixes it
        for a terminal launch and not for a double-clicked ``.app``,
        which inherits no shell — the same trap the Neo4j runtime flag
        fell into, where the documented fix could not be carried out by
        the people who needed it.
        """
        if sys.platform != "darwin":
            return
        present = [d for d in _MAC_LIBRARY_DIRS if Path(d).is_dir()]
        if not present:
            return
        current = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
        parts = [p for p in current.split(":") if p]
        # Appended, not prepended: a path the user set themselves is a
        # deliberate choice about which copy of pango to load.
        for directory in present:
            if directory not in parts:
                parts.append(directory)
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(parts)

    @classmethod
    def _load(cls) -> Any:
        """Import WeasyPrint once, remembering either outcome."""
        with cls._lock:
            if cls._html_class is not None:
                return cls._html_class
            if cls._failure:
                raise PdfUnavailable(cls._failure)
            cls._prepare_library_path()
            try:
                from weasyprint import HTML  # noqa: PLC0415 — deferred on purpose
            except Exception as exc:  # noqa: BLE001 — cffi raises OSError, not ImportError
                # The message WeasyPrint raises names a missing
                # ``.dylib``, which tells somebody nothing about what to
                # do. Name the package instead.
                cls._failure = (
                    "PDF export needs the pango graphics libraries, which are "
                    f"not installed. Run `{INSTALL_HINT}` and restart igni. "
                    f"({exc.__class__.__name__}: {exc})"
                )
                logger.info("weasyprint unavailable: %s", exc)
                raise PdfUnavailable(cls._failure) from exc
            cls._html_class = HTML
            return HTML

    @classmethod
    def is_available(cls) -> bool:
        """Whether a PDF could be produced, without producing one."""
        try:
            cls._load()
        except PdfUnavailable:
            return False
        return True

    @classmethod
    def render(cls, html: str, *, base_url: str | None = None) -> bytes:
        """One page of HTML, as PDF bytes.

        ``base_url`` is where relative links resolve from. The export is
        self-contained, so nothing should be fetched — but WeasyPrint
        will try the network for anything that slips through, and a
        base of ``None`` makes that fail fast instead of slowly.
        """
        html_class = cls._load()
        return bytes(html_class(string=html, base_url=base_url).write_pdf())
