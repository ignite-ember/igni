"""LoopProgressTool — per-iteration progress scratchpad for ``/loop``.

The ``/loop`` primitive re-fires the same prompt every iteration —
it has no memory of which work the previous iteration completed.
This tool gives the model a project-local SQLite table to write to
so iteration N can read what iteration N-1 finished and pick up
from there.

Extracted from :mod:`ember_code.core.tools.loop` so the agent-facing
loop-control tool (``LoopTools``) and the loop-progress scratchpad
each live in their own module. Both are still exposed from the
canonical ``ember_code.core.tools.loop`` path via re-export for
backward compatibility.

Typical pattern, inside the loop prompt::

    Read every ``loop_progress_list`` row. For each section in
    the file that *doesn't* appear in the list, verify it,
    ``loop_progress_set("section_X", "verified ok")``, then
    stop. If every section already appears, say DONE.

The tool's run_id is read from :attr:`Session.loop_run_id` on
every call, so it always operates on the *current* loop's
progress; entries from a previous (different ``run_id``) loop
stay in the DB but are invisible. Without an active loop, each
method returns an error string explaining there's nothing to
write to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

if TYPE_CHECKING:
    from ember_code.core.session.core import Session


class LoopProgressTool(Toolkit):
    """Per-iteration progress scratchpad for the active ``/loop``."""

    def __init__(self, session: Session) -> None:
        super().__init__(name="ember_loop_progress")
        self._session = session
        self.register(self.loop_progress_get)
        self.register(self.loop_progress_set)
        self.register(self.loop_progress_list)
        self.register(self.loop_progress_delete)
        self.register(self.loop_progress_clear)

    def _run_id_or_error(self) -> tuple[str, None] | tuple[None, str]:
        run_id = self._session.loop_run_id
        if not run_id:
            return None, (
                "ERROR: no /loop is active — start one with loop_start() "
                "before using loop_progress_*."
            )
        return run_id, None

    async def loop_progress_get(self, key: str) -> str:
        """Read the value previously stored for ``key`` in the
        current loop run. Returns the value verbatim, or an empty
        string when the key has never been set."""
        run_id, err = self._run_id_or_error()
        if err:
            return err
        value = await self._session.loop_progress_store.get(run_id, key)
        return value or ""

    async def loop_progress_set(self, key: str, value: str) -> str:
        """Record progress for ``key`` in the current loop run.

        Idempotent — calling ``set`` twice on the same key just
        replaces the value (so the model can append notes across
        iterations without throwing on the unique constraint).
        """
        run_id, err = self._run_id_or_error()
        if err:
            return err
        await self._session.loop_progress_store.set(run_id, key, value)
        return f"Saved progress for {key!r}."

    async def loop_progress_list(self) -> str:
        """List every (key, value) for the current loop run, ordered
        chronologically. Returns ``"No progress recorded."`` when
        nothing has been written yet — that's the model's signal
        that this is iteration 1."""
        run_id, err = self._run_id_or_error()
        if err:
            return err
        rows = await self._session.loop_progress_store.list(run_id)
        if not rows:
            return "No progress recorded."
        lines = [f"- {k}: {v}" for k, v in rows]
        return "Progress so far:\n" + "\n".join(lines)

    async def loop_progress_delete(self, key: str) -> str:
        """Remove a single progress entry. Useful when the model
        decides a previous iteration's verdict was wrong and wants
        to re-do the work."""
        run_id, err = self._run_id_or_error()
        if err:
            return err
        ok = await self._session.loop_progress_store.delete(run_id, key)
        return f"Deleted {key!r}." if ok else f"No entry for {key!r}."

    async def loop_progress_clear(self) -> str:
        """Wipe every progress entry for the current loop run.

        Rarely useful — the typical workflow keeps progress across
        iterations. Calling this mid-loop resets the model's
        memory of completed work, so iteration N+1 starts from
        scratch as if it were iteration 1.
        """
        run_id, err = self._run_id_or_error()
        if err:
            return err
        n = await self._session.loop_progress_store.clear(run_id)
        return f"Cleared {n} progress entr{'y' if n == 1 else 'ies'}."
