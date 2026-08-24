"""Lifecycle owner for the shared :class:`CodeIndex` + its three services.

Extracted from :class:`CodeIndexTools` so the toolkit facade doesn't
manage three lazy private fields inline. :class:`CodeIndexServices`
owns the on-disk :class:`CodeIndex` handle and the three service
wrappers (:class:`QueryService`, :class:`TreeService`, :class:`CypherService`)
that operate against it. Everything is lazy: no chroma directory is opened
until the first :meth:`query` / :meth:`tree` / :meth:`cypher` call.

The toolkit can inject a pre-built :class:`CodeIndex` (tests / advanced
callers) via ``explicit_index`` — the semantics of :meth:`close`
match the historical toolkit behaviour: whichever :class:`CodeIndex`
the composition holds gets closed, regardless of who built it.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.code_index.embedder import LiveEmbedder
from ember_code.core.code_index.index import CodeIndex
from ember_code.core.tools.codeindex.cypher_service import CypherService
from ember_code.core.tools.codeindex.query_service import QueryService
from ember_code.core.tools.codeindex.tree_service import TreeService


class CodeIndexServices:
    """Composition of :class:`CodeIndex` + :class:`QueryService` +
    :class:`TreeService` + :class:`CypherService`.

    Constructed once per toolkit; the four underlying objects are
    built on first access so ``__init__`` stays cheap (no disk I/O).
    Callers reach for :meth:`query`, :meth:`tree`, :meth:`cypher` —
    those methods materialise the underlying services on demand.
    """

    def __init__(
        self,
        *,
        project_dir: Path,
        data_dir: str | Path,
        explicit_index: CodeIndex | None = None,
    ) -> None:
        self._project_dir = project_dir
        self._data_dir = data_dir
        self._index: CodeIndex | None = explicit_index
        self._query_service: QueryService | None = None
        self._tree_service: TreeService | None = None
        self._cypher_service: CypherService | None = None

    @property
    def index(self) -> CodeIndex:
        """The :class:`CodeIndex` handle, building it on first access.

        Exposed so :class:`CodeIndexTools` can forward its historical
        ``_explicit_index`` attribute to tests that monkeypatch
        ``search`` on the underlying index.
        """
        if self._index is None:
            # LiveEmbedder, not the default: all-MiniLM-L6-v2 at 384 dims is
            # exactly what the ``chunk_embedding`` vector index expects, and
            # without it the chunks are hashes and semantic search is dead.
            self._index = CodeIndex(
                project=self._project_dir, data_dir=self._data_dir, embedder=LiveEmbedder()
            )
        return self._index

    def query(self) -> QueryService:
        """Return the shared :class:`QueryService`, building it on first use."""
        if self._query_service is None:
            self._query_service = QueryService(self.index)
        return self._query_service

    def tree(self) -> TreeService:
        """Return the shared :class:`TreeService`, building it on first use."""
        if self._tree_service is None:
            self._tree_service = TreeService(self.index)
        return self._tree_service

    def cypher(self) -> CypherService:
        """Return the shared :class:`CypherService`, building it on first use."""
        if self._cypher_service is None:
            self._cypher_service = CypherService(self.index)
        return self._cypher_service

    async def close(self) -> None:
        """Close the underlying :class:`CodeIndex` if one was opened.

        Matches the historical toolkit semantics: whichever :class:`CodeIndex`
        this composition holds gets closed, whether it was injected or
        self-built. No-op when neither :meth:`query` nor :meth:`tree`
        has ever been called AND no explicit index was passed in.
        """
        if self._index is not None:
            await self._index.close()
