"""Live apply-progress state for the CodeIndex panel poll.

Extracted from four ``_``-prefixed fields (``_applying``,
``_apply_done``, ``_apply_total``, ``_apply_step``) plus two
helpers (``_on_apply_progress``, ``_reset_apply_progress``) on
the old :class:`CodeIndexSyncManager`. Now a dedicated class with:

* :meth:`update` — the callback plugged into ``apply_delta``'s
  ``on_progress`` argument (bound method — no glue lambda on
  the manager).
* :meth:`active_scope` — a context manager that flips
  :attr:`active` on entry and off on exit, replacing the raw
  ``try / finally: self._applying = False`` block in the old
  ``_sync_locked``.
* :meth:`snapshot` — the fragment fed into
  :class:`SyncProgressSnapshot`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import BaseModel


class ApplyProgressSnapshot(BaseModel):
    """Wire fragment for :class:`SyncProgressSnapshot`.

    A Pydantic model, not a bare tuple, so
    :meth:`CodeIndexSyncManager.progress_snapshot` can copy
    fields by name into the outer :class:`SyncProgressSnapshot`.
    """

    applying: bool = False
    apply_done: int = 0
    apply_total: int = 0
    apply_step: str = ""


class ApplyProgress:
    """Live counters + label for an in-flight ``apply_delta``.

    Reset on every :meth:`active_scope` entry so a stale
    progress row from a previous run never bleeds into the next
    ``codeindex_status`` poll.
    """

    def __init__(self) -> None:
        self._active: bool = False
        self._done: int = 0
        self._total: int = 0
        self._step: str = ""

    @property
    def active(self) -> bool:
        return self._active

    def reset(self) -> None:
        """Zero the counters. Used on sync start."""
        self._active = False
        self._done = 0
        self._total = 0
        self._step = ""

    def update(self, done: int, total: int, label: str) -> None:
        """Callback fed to ``apply_delta`` — surfaces per-item progress.

        The TUI polls ``codeindex_status`` while a
        ``/codeindex resync`` is running; reading these three
        fields keeps the busy label moving instead of stuck at
        ``Resyncing (full snapshot)…`` for the ~30-90s an apply
        takes on a fresh checkout.
        """
        self._done = done
        self._total = total
        self._step = label

    @contextmanager
    def active_scope(self) -> Iterator[None]:
        """Flip ``active`` on entry and off on exit.

        Replaces the ``try / finally: self._applying = False``
        idiom on the old manager. Counters are NOT reset on exit
        so the panel keeps rendering the last frame briefly
        while the next poll arrives; :meth:`reset` at sync-start
        clears them for the next run.
        """
        self._active = True
        try:
            yield
        finally:
            self._active = False

    def snapshot(self) -> ApplyProgressSnapshot:
        return ApplyProgressSnapshot(
            applying=self._active,
            apply_done=self._done,
            apply_total=self._total,
            apply_step=self._step,
        )


__all__ = ["ApplyProgress", "ApplyProgressSnapshot"]
