"""Composer-paste code search: exact-substring lookup across the project.

:class:`SearchController` is a thin coordinator that composes two
collaborators picked at construction time:

* an :class:`LruSearchCache` (from :mod:`search_cache`) — the
  bounded MRU response cache keyed by
  ``(project_root, max_results, snippet)``.
* a :class:`SearchStrategy` (from :mod:`search_strategies`) —
  either :class:`RgSearchStrategy` when ``rg`` is on ``PATH``,
  or :class:`PythonWalkSearchStrategy` as the pure-Python
  fallback. Chosen once via
  :meth:`SearchStrategy.pick` so the ``shutil.which`` probe
  doesn't run per-call.

Wire schemas (:class:`SearchCodeMatch`, :class:`SearchCodeResult`)
live in :mod:`ember_code.backend.schemas_search`; they are
re-exported from this module so ``from
ember_code.backend.server_search import SearchCodeResult`` keeps
working for existing callers.

Match mode is **exact substring** — no normalisation, no fuzzy.
Multi-line snippets become a single literal pattern.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.backend.schemas_search import SearchCodeMatch, SearchCodeResult
from ember_code.backend.search_cache import LruSearchCache
from ember_code.backend.search_strategies import SearchStrategy

if TYPE_CHECKING:
    from ember_code.core.session import Session


# Public re-exports so existing imports of the wire schemas from
# ``server_search`` keep working after the schemas moved into a
# dedicated sibling module.
__all__ = [
    "SearchCodeMatch",
    "SearchCodeResult",
    "SearchController",
]


class SearchController:
    """Composer-paste code search for one :class:`Session`.

    Owns an :class:`LruSearchCache` instance and a chosen
    :class:`SearchStrategy` instance — both injectable via the
    constructor for tests, both defaulted for production.
    """

    def __init__(
        self,
        session: Session,
        *,
        strategy: SearchStrategy | None = None,
        cache: LruSearchCache | None = None,
    ) -> None:
        self._session = session
        self._strategy = strategy if strategy is not None else SearchStrategy.pick()
        self._cache = cache if cache is not None else LruSearchCache()

    def search_code(self, snippet: str, max_results: int = 20) -> SearchCodeResult:
        """Find exact-substring occurrences of ``snippet`` across
        the project.

        Delegates to the configured :class:`SearchStrategy` after a
        cache lookup. Short snippets (< 5 chars) short-circuit to an
        empty result — the composer paste heuristic isn't useful
        below that length and matches would be dominated by noise.
        """
        snippet = (snippet or "").strip()
        if len(snippet) < 5:
            return SearchCodeResult()

        project_root = Path(self._session.project_dir).resolve()
        cache_key = hashlib.sha1(
            f"{project_root}\0{max_results}\0{snippet}".encode("utf-8", "ignore")
        ).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        snippet_lines = snippet.count("\n") + 1
        payload = self._strategy.search(project_root, snippet, snippet_lines, max_results)
        self._cache.put(cache_key, payload)
        return payload
