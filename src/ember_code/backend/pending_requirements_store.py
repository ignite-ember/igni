"""Store for HITL requirements paused waiting on a user decision.

Extracted from :mod:`ember_code.backend.server_pause` — the two
dicts ``_pending_requirements`` and ``_auto_resolved_requirements``
previously lived as attributes on :class:`BackendServer` and were
mutated from three separate modules (``server_pause``,
``hitl_controller``, and several tests). This class owns both dicts
and exposes the mutation API as methods so a future audit for stray
``backend._pending_requirements[x] = y`` has one grep target and
one place to add invariant guards.

Invariant enforced by the store: a requirement id lives in
``_pending`` XOR in ``_auto_resolved`` — never both. The audit
flagged the previous state (two dicts mutated independently) as a
correctness seam because ``HitlController._merge_auto_resolved`` and
``server_pause.drop_pending_for_run`` both had to sweep both dicts
in the right order.
"""

from __future__ import annotations

import logging
from typing import Any

from ember_code.backend.schemas_pause import PendingRequirement

logger = logging.getLogger(__name__)


class PendingRequirementsStore:
    """Two-bucket store for paused-and-not-yet-resolved requirements.

    Bucket A (``_pending``): requirement is waiting for a user
    decision. Populated by :meth:`PauseHandler.handle` (via
    :meth:`register`); drained by :meth:`HitlController.resolve_batch`
    (via :meth:`pop`).

    Bucket B (``_auto_resolved``): requirement was decided by the
    permission evaluator BEFORE the user got a chance. Stashed by
    :meth:`PauseHandler.handle` for mixed pauses (some reqs need
    the user, others don't) so ``resolve_hitl_batch`` can merge
    them into the eventual ``acontinue_run`` call. Drained by
    :meth:`drain_auto_resolved`.
    """

    def __init__(self) -> None:
        """Empty store — one instance per :class:`BackendServer`."""
        self._pending: dict[str, PendingRequirement] = {}
        self._auto_resolved: dict[str, list[Any]] = {}

    # ── Read/mutate the pending bucket ────────────────────────────

    def register(self, req_id: str, entry: PendingRequirement) -> None:
        """Stash a requirement waiting for the user."""
        self._pending[req_id] = entry

    def pop(self, req_id: str) -> PendingRequirement | None:
        """Remove and return the pending requirement, or ``None`` if
        unknown. Used by both the sub-agent-claim path (drop the
        main-team entry to keep the store clean) and the user-
        decision resolver."""
        return self._pending.pop(req_id, None)

    def has(self, req_id: str) -> bool:
        """True when the id is currently pending. Kept for the
        test-only ``'x' in server._pending_requirements`` style
        assertion via the compat view (see :meth:`pending_ids`)."""
        return req_id in self._pending

    def pending_ids(self) -> list[str]:
        """Snapshot of currently-pending requirement ids."""
        return list(self._pending.keys())

    def sweep_run(self, run_id: str) -> int:
        """Remove every pending entry tied to a finished run.

        Called when a run completes/errors without going through
        ``resolve_hitl_batch`` (which would've popped them). Also
        drops the corresponding ``_auto_resolved`` bucket so the
        pair invariant holds. Returns the number of pending
        entries evicted.
        """
        stale = [rid for rid, entry in self._pending.items() if entry.run_id == run_id]
        for rid in stale:
            self._pending.pop(rid, None)
        self._auto_resolved.pop(run_id, None)
        if stale:
            logger.debug(
                "sweep_run: dropped %d stale requirement(s) for run_id=%s",
                len(stale),
                run_id,
            )
        return len(stale)

    # ── Read/mutate the auto-resolved bucket ──────────────────────

    def stash_auto_resolved(self, run_id: str, reqs: list[Any]) -> None:
        """Append evaluator-resolved requirements to the run's bucket.

        Used by :meth:`PauseHandler.handle` on the mixed-pause path
        so ``resolve_hitl_batch`` can drain them alongside the
        user-resolved reqs."""
        if not reqs:
            return
        self._auto_resolved.setdefault(run_id, []).extend(reqs)

    def drain_auto_resolved(self, run_id: str | None) -> list[Any]:
        """Pop and return the auto-resolved list for a run.

        Returns ``[]`` when ``run_id`` is ``None`` or absent — used
        by :meth:`HitlController._merge_auto_resolved` which must always
        merge cleanly even for runs that never triggered an
        auto-decision."""
        if run_id is None:
            return []
        return self._auto_resolved.pop(run_id, [])

    def auto_resolved_snapshot(self) -> dict[str, list[Any]]:
        """Copy of the auto-resolved bucket for test assertions.

        Tests compare ``server._auto_resolved_requirements == {}``
        after a drain; this method backs the compat read-shim on
        :class:`BackendServer`."""
        return self._auto_resolved

    # ── Backward-compat dict views ────────────────────────────────
    #
    # ``BackendServer._pending_requirements`` and
    # ``_auto_resolved_requirements`` used to be raw dicts. Tests +
    # :class:`HitlController` still expect a dict-like surface
    # (``.pop``, ``in``, item assignment). We expose the underlying
    # dicts here so the compat properties on BackendServer forward
    # to them directly — no proxy class needed. The mutation API
    # above is still the preferred surface for new code.

    @property
    def pending_dict(self) -> dict[str, PendingRequirement]:
        """Raw pending dict — kept for the compat read-shim on
        :class:`BackendServer`."""
        return self._pending

    @property
    def auto_resolved_dict(self) -> dict[str, list[Any]]:
        """Raw auto-resolved dict — kept for the compat read-shim."""
        return self._auto_resolved
