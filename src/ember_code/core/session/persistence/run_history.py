"""A session's runs, written as igni means them: exactly the ones it holds.

Agno 2.6 kept a session's runs inside its row, so writing the session
wrote its history: clear ``session.runs`` and save, and the history was
gone. Agno 3 keeps each run in a row of its own (the runs table, with
``run_id`` its key), and ``upsert_session`` writes the session's row and
nothing else — a session saved with fewer runs keeps every run it had.

igni rewrites history in four places: compaction clears it, a rewind
cuts it at a run, a fork copies it under a new id, and an interrupted
approval closes one run. Each saved the session and relied on the runs
going with it; under Agno 3 each silently did nothing. They go through
:class:`RunHistory` instead, which writes the session's row, every run it
holds in order, and deletes the stored runs it no longer holds.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any


class RunHistory:
    """Writes a session's runs to an Agno 3 database, the session's row too."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def save(self, session: Any) -> None:
        """Store ``session`` with exactly ``session.runs``, in that order."""
        await self._db.upsert_session(session, deserialize=True)
        await self.replace_runs(session)

    @classmethod
    async def save_for(cls, agent: Any, session: Any) -> None:
        """What :meth:`save` does, with the session's row saved the agent's way.

        ``Agent.asave_session`` is Agno's own save (it drops the run-scoped
        keys from the session state first); the runs then follow.
        """
        await agent.asave_session(session)
        await cls(agent.db).replace_runs(session)

    async def replace_runs(self, session: Any) -> None:
        """Make the stored runs of ``session`` exactly ``session.runs``, in order."""
        runs = list(getattr(session, "runs", None) or [])
        kept = {run.run_id for run in runs}
        stored = await self._stored_run_ids(session.session_id)
        gone = [run_id for run_id in stored if run_id not in kept]
        if gone:
            await self._db.delete_runs(gone)
        for index, run in enumerate(runs):
            await self.save_run(session, run, run_index=index)

    async def save_run(self, session: Any, run: Any, *, run_index: int | None = None) -> None:
        """Store one run of ``session`` (a changed status, say)."""
        await self._db.upsert_run(
            run,
            session_id=session.session_id,
            user_id=getattr(session, "user_id", None),
            run_index=run_index,
        )

    async def _stored_run_ids(self, session_id: str) -> list[str]:
        found = await self._db.get_runs(session_id=session_id, deserialize=True)
        runs = found[0] if isinstance(found, tuple) else found
        return [run.run_id for run in runs or [] if getattr(run, "run_id", None)]

    @staticmethod
    def copied(runs: list[Any]) -> list[Any]:
        """``runs`` with fresh ids, for another session.

        A run's id is the runs table's key, so a fork that kept the
        source's ids would overwrite the source's rows rather than add
        its own. A member's run points at its parent by id, so the links
        are renamed with the runs.
        """
        renamed = {run.run_id: str(uuid.uuid4()) for run in runs if getattr(run, "run_id", None)}
        copies = []
        for run in runs:
            twin = copy.deepcopy(run)
            twin.run_id = renamed.get(run.run_id, run.run_id)
            parent = getattr(twin, "parent_run_id", None)
            if parent in renamed:
                twin.parent_run_id = renamed[parent]
            copies.append(twin)
        return copies


__all__ = ["RunHistory"]
