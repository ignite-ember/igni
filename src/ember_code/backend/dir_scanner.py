"""Filesystem directory scanner for the GUI folder browser.

Extracted from :mod:`ember_code.backend.rpc_router` where it used to
live as a private ``_scan_dirs`` static method that ignored ``self``
(a free function in class clothing). It now owns its inputs as
instance attributes and exposes a blocking :meth:`scan` and an async
:meth:`scan_async` wrapper.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ember_code.backend.schemas_rpc import DirListResult


class DirScanner:
    """Return a :class:`DirListResult` for one directory.

    Same trust level as ``run_shell`` (local user over loopback).
    Dot-dirs are filtered unless ``show_hidden`` — the browser is for
    picking project roots, not spelunking.
    """

    def __init__(self, path: Path, show_hidden: bool) -> None:
        self._path = path
        self._show_hidden = show_hidden

    def scan(self) -> DirListResult:
        base = self._path.expanduser()
        try:
            base = base.resolve()
            dirs = sorted(
                (
                    p.name
                    for p in base.iterdir()
                    if p.is_dir() and (self._show_hidden or not p.name.startswith("."))
                ),
                key=str.lower,
            )
        except (OSError, PermissionError) as exc:
            return DirListResult(
                path=str(base),
                parent=str(base.parent),
                dirs=[],
                home=str(Path.home()),
                error=str(exc),
            )
        return DirListResult(
            path=str(base),
            parent=str(base.parent) if base != base.parent else "",
            dirs=dirs,
            home=str(Path.home()),
            error="",
        )

    async def scan_async(self) -> DirListResult:
        """Off-thread wrapper — the RPC dispatcher awaits this."""
        return await asyncio.to_thread(self.scan)
