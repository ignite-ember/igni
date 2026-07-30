"""Ring buffer of recent :class:`ActivityEntry` rows.

Extracted from the ``self._activity`` list + ``self._activity_limit``
integer + trim-on-overflow snippet on the old
:class:`CodeIndexSyncManager`. Now a small class with a fixed
window size; the coordinator just calls :meth:`record` after every
sync attempt and reads :meth:`recent` when the panel polls.
"""

from __future__ import annotations

from ember_code.core.code_index.sync.schemas import ActivityEntry


class SyncActivityLog:
    """Bounded newest-first log of recent sync attempts.

    Not persisted — cheap to keep in memory, wiped on session
    restart. The panel's activity list reads :meth:`recent`
    directly.
    """

    DEFAULT_LIMIT: int = 20

    def __init__(self, *, limit: int | None = None) -> None:
        self._limit = limit if limit is not None else self.DEFAULT_LIMIT
        self._entries: list[ActivityEntry] = []

    def record(self, entry: ActivityEntry) -> None:
        """Append ``entry`` and trim to :attr:`DEFAULT_LIMIT`."""
        self._entries.append(entry)
        if len(self._entries) > self._limit:
            self._entries = self._entries[-self._limit :]

    def recent(self) -> list[ActivityEntry]:
        """Most-recent entries first (newest → oldest)."""
        return list(reversed(self._entries))


__all__ = ["SyncActivityLog"]
