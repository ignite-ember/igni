"""File-completion (``@``-mention picker) service.

Extracted from :mod:`ember_code.backend.rpc_router` — the RPC router
used to carry a lazily-built ``FileIndex`` as its only real state
beyond the four injected refs (backend/transport/login/push). Moving
the cache onto its own service class clarifies both classes: the
router becomes stateless composition, this class owns the index
lifecycle.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.backend.schemas_rpc import FileCompletion
from ember_code.core.utils.file_index import FileIndex


class FileCompletionService:
    """Warm-cache wrapper around :class:`FileIndex`.

    ``FileIndex`` is expensive to build (walks the project tree). The
    RPC handler pins a single service instance for the process
    lifetime so repeated ``complete_files`` calls hit the warm index.
    """

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir
        self._file_index: FileIndex | None = None

    async def complete(self, query: str, limit: int) -> FileCompletion:
        if self._file_index is None:
            self._file_index = FileIndex(self._project_dir)
        await self._file_index.ensure_loaded()
        matches, total = self._file_index.match_with_total(query, limit=limit)
        return FileCompletion(matches=matches, total=total)
