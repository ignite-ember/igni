"""Journal-style facade over :class:`PendingMessageStore`.

Extracted out of the pre-persist / mark-completed / drop-on-continue
sequence in the old ``server_run.run_message_locked``.

Concerns owned:

* The pending-message lifecycle (record → mark completed / discard).
* The one-shot "drop these ids on the next run_message"
  handoff between ``detect_interrupted_run`` and the run pipeline
  (previously two direct attribute writes on ``BackendServer``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.core.session.pending_messages import PendingMessageStore


class PendingMessageJournal:
    """Own the pending-user-message table for one session.

    Wraps the low-level :class:`PendingMessageStore` with methods
    named for the run pipeline's steps + a small state slot for
    the ids the previous ``detect_interrupted_run`` call flagged
    for drop."""

    def __init__(self, store: PendingMessageStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id
        self._ids_to_drop: list[str] = []

    @property
    def store(self) -> PendingMessageStore:
        """Direct access to the underlying store — kept for
        server_context / detect_interrupted_run which still call
        the low-level methods (``alist_pending``, ``adiscard``,
        ``arecord_received``) directly."""
        return self._store

    async def record(self, text: str) -> str:
        """Pre-persist a user message before ``team.arun``. Returns
        the opaque row id so the caller can mark it completed on
        the success path."""
        return await self._store.arecord_received(self._session_id, text)

    async def mark_completed(self, pending_id: str) -> None:
        """Flip a pre-persisted row to ``completed`` — called on the
        natural end-of-run path."""
        await self._store.amark_completed(pending_id)

    def queue_drops(self, ids: list[str]) -> None:
        """Stash pending-row ids surfaced by ``detect_interrupted_run``
        so the next ``run_message`` can discard them after the
        agent acknowledges the resume.

        Populated as a side-effect of
        :meth:`RunController.set_interrupted_summary` — the typed
        :class:`InterruptedRunSummary` carries
        ``pending_ids_to_drop`` which the run controller passes
        through to this queue."""
        self._ids_to_drop = list(ids)

    @property
    def queued_drop_ids(self) -> list[str]:
        """Snapshot of the currently-queued drop ids.

        Kept public (rather than reaching for ``_ids_to_drop``)
        so tests can pin the two-step lifecycle
        (``detect_interrupted_run`` queues → ``run_message``
        drains) without touching a private attr."""
        return list(self._ids_to_drop)

    async def drain_queued_drops(self) -> None:
        """Discard every queued pending-row id and clear the queue.

        Called at the start of ``run_message`` after the interrupted
        summary has been consumed. A second restart before the user
        actually responds must NOT re-surface these rows, so we
        discard here rather than on ``detect_interrupted_run`` (the
        FE needs to fetch them once first via
        ``get_pending_messages`` to render the interrupted question
        in the conversation pane)."""
        if not self._ids_to_drop:
            return
        for pending_id in self._ids_to_drop:
            await self._store.adiscard(pending_id)
        self._ids_to_drop = []
