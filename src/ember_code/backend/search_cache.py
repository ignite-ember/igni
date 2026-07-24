"""Bounded LRU-ish cache for :class:`SearchController` results.

:class:`LruSearchCache` owns both the MRU touch (``pop + re-set``)
and the size-cap enforcement so the controller sees one collaborator
rather than a raw dict + a free-function helper.

Insertion order in Python dicts doubles as MRU order — a
``pop + re-set`` promotes an entry to the newest slot, and
``next(iter(cache))`` returns the oldest for eviction.
"""

from __future__ import annotations

from ember_code.backend.schemas_search import SearchCodeResult


class LruSearchCache:
    """Bounded MRU cache keyed by SHA-1 of ``(project_root, max_results, snippet)``.

    Small values (a few dozen entries max, each a compact JSON-shaped
    Pydantic model) — this is a in-process response cache, not a
    long-lived on-disk store.
    """

    def __init__(self, max_entries: int = 64) -> None:
        self._max_entries = max_entries
        self._entries: dict[str, SearchCodeResult] = {}

    def get(self, key: str) -> SearchCodeResult | None:
        """Return the cached payload for ``key`` and promote it to MRU.

        Returns ``None`` on a miss. The pop + re-set dance uses the
        fact that Python dicts preserve insertion order so the
        just-touched key ends up at the tail (newest) position.
        """
        cached = self._entries.get(key)
        if cached is None:
            return None
        self._entries.pop(key, None)
        self._entries[key] = cached
        return cached

    def put(self, key: str, value: SearchCodeResult) -> None:
        """Insert ``value`` under ``key`` and trim to ``max_entries``.

        Oldest entries (front of the dict) fall off first.
        """
        self._entries[key] = value
        while len(self._entries) > self._max_entries:
            self._entries.pop(next(iter(self._entries)))
