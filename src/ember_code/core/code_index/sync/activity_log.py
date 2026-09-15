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
        """Append ``entry`` and trim to :attr:`DEFAULT_LIMIT`.

        A skip that repeats the previous entry's reason replaces it
        rather than being appended. The HEAD watcher polls at 1Hz, so
        a standing condition — "no igni server configured", a
        repository that is not connected — produced one row per
        second and refilled all twenty slots every twenty seconds.
        The log could then only ever show the last twenty seconds of
        the same sentence, and any real sync before it had already
        been evicted.

        Only consecutive *skips* with an identical reason collapse.
        Errors and successes always append: twenty failures in a row
        are twenty separate facts, and the gap between them is
        information.
        """
        previous = self._entries[-1] if self._entries else None
        if (
            previous is not None
            and entry.skipped
            and previous.skipped
            and entry.reason == previous.reason
        ):
            self._entries[-1] = entry
            return
        self._entries.append(entry)
        if len(self._entries) > self._limit:
            self._entries = self._entries[-self._limit :]

    def recent(self) -> list[ActivityEntry]:
        """Most-recent entries first (newest → oldest)."""
        return list(reversed(self._entries))


__all__ = ["SyncActivityLog"]
