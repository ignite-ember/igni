"""Session persistence — listing, naming, and resuming sessions."""

import asyncio
import logging
import time
import uuid
from typing import Any

from agno.db.base import SessionType
from agno.session.agent import AgentSession

from ember_code.core.session.event_log_schema import SessionEvent
from ember_code.core.tools.todo import TodoItemWire

logger = logging.getLogger(__name__)


class SessionPersistence:
    """Handles session listing, naming, and metadata via Agno's DB."""

    def __init__(self, db: Any, session_id: str):
        self.db = db
        self.session_id = session_id
        # Serializes ``session_data`` writes so concurrent callers
        # (event-log append, todo save, plan-decisions save) can't
        # clobber each other. Every ``_upsert_session_data_key``
        # invocation is a load-modify-write against Agno's session
        # row; without the lock, two writes racing on the merge step
        # would both start from the same pre-image and the second to
        # ``upsert_session`` would win — silently dropping the first
        # write's key. Cheap: ``asyncio.Lock`` is uncontended on the
        # hot path (single-writer sessions), only bites in the rare
        # multi-persist-in-flight case.
        self._session_data_lock = asyncio.Lock()

    @staticmethod
    def _session_to_wire(s: Any) -> dict[str, Any]:
        """Serialize one Agno session row to the wire shape the
        FE consumes: ``{session_id, name, created_at, updated_at,
        run_count, summary, agent_name}``."""
        run_count = len(s.runs) if s.runs else 0
        summary = ""
        if s.summary and hasattr(s.summary, "summary"):
            summary = s.summary.summary or ""
        agent_name = ""
        if s.agent_data and isinstance(s.agent_data, dict):
            agent_name = s.agent_data.get("name", "")
        name = ""
        if s.session_data and isinstance(s.session_data, dict):
            name = s.session_data.get("session_name", "")
        return {
            "session_id": s.session_id,
            "name": name,
            "created_at": s.created_at or 0,
            "updated_at": s.updated_at or 0,
            "run_count": run_count,
            "summary": summary,
            "agent_name": agent_name,
        }

    async def list_sessions(self, limit: int | None = None) -> list[dict[str, Any]]:
        """List sessions from the Agno database.

        ``limit=None`` (the default) returns every session for this
        project. Callers can pass an int to cap; the CLI's
        pre-launch session preview does this to keep boot time flat.

        Skips sub-agent scratch sessions (visualizer, editor, every
        specialist in the pool). Sub-agents share the top-level DB
        so paused runs can be resumed via ``acontinue_run`` — see
        ``AgentPool.__init__``'s ``_db`` — but they should NEVER
        appear as chats in the user-facing session list. The
        top-level session is built with ``Agent(name="ember")``
        (see ``_build_main_agent``), so ``agent_id == "ember"``
        is the discriminator. Anything else is scratch.
        """
        if not self.db:
            return []
        try:
            sessions = await self.db.get_sessions(
                session_type=SessionType.AGENT,
                limit=limit,
                sort_by="updated_at",
                sort_order="desc",
                deserialize=True,
            )
            if isinstance(sessions, tuple):
                sessions = sessions[0]

            results = []
            for s in sessions:
                agent_id = getattr(s, "agent_id", "") or ""
                if agent_id and agent_id != "ember":
                    continue
                results.append(self._session_to_wire(s))
            return results
        except Exception as exc:
            logger.debug("Failed to list sessions: %s", exc)
            return []

    async def auto_name(self, executor: Any) -> None:
        """Ask Agno to auto-generate a session name from conversation."""
        try:
            if hasattr(executor, "aset_session_name"):
                await executor.aset_session_name(
                    session_id=self.session_id,
                    autogenerate=True,
                )
        except Exception as exc:
            logger.debug("Failed to auto-name session: %s", exc)
            pass

    async def rename(self, new_name: str) -> None:
        """Manually rename the current session."""
        if not self.db:
            return
        try:

            await self.db.rename_session(
                session_id=self.session_id,
                session_type=SessionType.AGENT,
                session_name=new_name,
            )
        except Exception as exc:
            logger.debug("Failed to rename session: %s", exc)
            pass

    async def fork(self, name: str | None = None) -> str:
        """Clone the current session under a fresh ``session_id``.

        Reads the source session from Agno's DB, mints a new UUID,
        copies every field (``session_data`` / ``team_data`` /
        ``metadata`` / ``runs`` / ``summary``) under the new id with
        fresh ``created_at`` / ``updated_at`` stamps, optionally
        renames it, and upserts it. Memories aren't copied — they're
        user-scoped on disk so the new session inherits them
        automatically.

        Returns the new ``session_id``. Raises ``RuntimeError`` if no
        DB is configured or the source session can't be loaded.
        """
        if not self.db:
            raise RuntimeError("session store unavailable")

        source = await self.db.get_session(
            session_id=self.session_id,
            session_type=SessionType.AGENT,
            deserialize=True,
        )
        if source is None:
            raise RuntimeError(f"source session not found: {self.session_id}")

        # Match the 8-char prefix scheme used elsewhere in the
        # codebase (``core.py``'s fresh-session mint). The full
        # ``uuid.uuid4().hex`` form was correct technically but read
        # as a 32-char wall of hex in the UI.
        new_id = str(uuid.uuid4())[:8]
        now = int(time.time())
        # ``source`` is a freshly-loaded copy from the DB — we own it,
        # so mutating in place is safe. Setting ``session_id`` to the
        # new value means ``upsert_session`` writes a NEW row keyed
        # by the new id (the original row is untouched).
        source.session_id = new_id
        source.created_at = now
        source.updated_at = now
        if name:
            sd = dict(source.session_data or {})
            sd["session_name"] = name
            source.session_data = sd

        await self.db.upsert_session(source, deserialize=True)
        return new_id

    async def get_name(self) -> str:
        """Get the current session's name from the database."""
        if not self.db:
            return ""
        try:

            session = await self.db.get_session(
                session_id=self.session_id,
                session_type=SessionType.AGENT,
                deserialize=True,
            )
            if session and session.session_data:
                return session.session_data.get("session_name", "")
        except Exception as exc:
            logger.debug("Failed to get session name: %s", exc)
            pass
        return ""

    async def load_plan_decisions(self) -> dict[str, str]:
        """Read the persisted ``{run_id: "approved"|"dismissed"}``
        map for this session. Empty dict when nothing was stored
        yet (fresh session) or on any DB error — callers treat
        absence as "no decision", which is the safe pending
        default."""
        if not self.db:
            return {}
        try:

            session = await self.db.get_session(
                session_id=self.session_id,
                session_type=SessionType.AGENT,
                deserialize=True,
            )
            if session and session.session_data:
                raw = session.session_data.get("plan_decisions")
                if isinstance(raw, dict):
                    return {
                        str(k): str(v)
                        for k, v in raw.items()
                        if isinstance(k, str) and v in ("approved", "dismissed")
                    }
        except Exception as exc:
            logger.debug("Failed to load plan decisions: %s", exc)
        return {}

    async def load_todos(self) -> list[dict]:
        """Read the persisted todo snapshot for this session.

        Each entry has the same wire shape ``todo_write``
        broadcasts (see :class:`TodoItemWire`): ``{content,
        status, activeForm}``. Returns an empty list when
        nothing's been written yet (fresh session) or on any DB
        / shape error — callers fall back to the plan's original
        task list, which is at least consistent with the user's
        last hand-approved state.
        """
        if not self.db:
            return []
        try:

            session = await self.db.get_session(
                session_id=self.session_id,
                session_type=SessionType.AGENT,
                deserialize=True,
            )
            if session and session.session_data:
                raw = session.session_data.get("todos")
                if isinstance(raw, list):
                    return _coerce_todo_snapshot(raw)
        except Exception as exc:
            logger.debug("Failed to load todos: %s", exc)
        return []

    async def load_event_log(self) -> list[dict]:
        """Read the persisted append-only event log for this
        session. Each entry has the :class:`SessionEvent` shape
        ``{seq, run_id, timestamp_ms, type, payload}``.

        Every persisted entry is round-tripped through
        :meth:`SessionEvent.from_wire` before being returned —
        stale / schema-drifted rows drop silently instead of
        surfacing as a ``KeyError`` deep in the splicer, and the
        output dicts come from one Pydantic definition (Rule 1).
        Returns [] on fresh session or any DB / shape error —
        callers treat missing log as "no non-message state to
        replay".
        """
        if not self.db:
            return []
        try:

            session = await self.db.get_session(
                session_id=self.session_id,
                session_type=SessionType.AGENT,
                deserialize=True,
            )
            if session and session.session_data:
                raw = session.session_data.get("event_log")
                if isinstance(raw, list):
                    out: list[dict] = []
                    for entry in raw:
                        if not isinstance(entry, dict):
                            continue
                        evt = SessionEvent.from_wire(entry)
                        if evt is not None:
                            out.append(evt.model_dump())
                    return out
        except Exception as exc:
            logger.debug("Failed to load event log: %s", exc)
        return []

    async def save_event_log(self, event_log: list[dict]) -> None:
        """Atomic-replace the session's event log. Called by
        :meth:`Session.append_event` after every append.

        Same merge semantics as :meth:`save_todos` — the wider
        ``session_data`` blob (session_name, todos,
        plan_decisions) is preserved. Best-effort: DB write
        failures log and return; the in-memory log still reaches
        live clients.
        """
        if not self.db:
            return
        try:
            await self._upsert_session_data_key("event_log", list(event_log))
        except Exception as exc:
            logger.debug("Failed to save event log: %s", exc)

    async def save_todos(self, todos: list[dict]) -> None:
        """Atomic-replace persisted todo snapshot in
        ``session_data``.

        Called after every ``todo_write`` so execution progress
        (e.g. task A → ``in_progress`` → ``completed``) survives
        BE restart. Without this, rehydration falls back to the
        plan's original task list — everything pending, all
        execution history erased.

        Merges with existing ``session_data`` (so a parallel
        ``save_plan_decisions`` doesn't clobber the todos and
        vice versa). If the session row doesn't yet exist in
        Agno's DB (fresh boot, zero turns), a minimal row is
        created so the snapshot still survives restart — without
        this fallback the write silently no-ops and the user
        loses execution state on the next launch.

        Best-effort: DB write failures log and return — the
        in-memory state and the live ``todos_updated`` broadcast
        still reach attached clients, only the restart-recovery
        is sacrificed.
        """
        if not self.db:
            return
        try:
            cleaned = _coerce_todo_snapshot(todos)
            await self._upsert_session_data_key("todos", cleaned)
        except Exception as exc:
            logger.debug("Failed to save todos: %s", exc)

    async def save_plan_decisions(self, decisions: dict[str, str]) -> None:
        """Write the ``{run_id: decision}`` map to the session's
        ``session_data`` blob. Merges with existing
        ``session_data`` (preserves ``session_name`` and friends);
        replaces the ``plan_decisions`` key wholesale.

        If the session row doesn't yet exist in Agno's DB
        (fresh boot, zero turns), a minimal row is created so
        the decision still survives restart. Without this
        fallback the write silently no-ops — discovered via the
        live-BE Playwright check: a raw ``approve_plan`` RPC
        against an empty BE returned the right shape and fired
        ``plan_decided``, but nothing landed in the DB.

        Best-effort: a DB write failure logs and returns — the
        in-memory state is still correct for the current
        session, the loss surfaces only on a later restart.
        Calling code should treat that as acceptable since the
        FE always gets the live broadcast regardless."""
        if not self.db:
            return
        try:
            cleaned = {
                str(k): str(v)
                for k, v in decisions.items()
                if isinstance(k, str) and v in ("approved", "dismissed")
            }
            await self._upsert_session_data_key("plan_decisions", cleaned)
        except Exception as exc:
            logger.debug("Failed to save plan decisions: %s", exc)

    async def _upsert_session_data_key(self, key: str, value: object) -> None:
        """Read the session row, set ``session_data[key] = value``,
        write it back. Creates a minimal row if the session has
        never been persisted yet.

        Single chokepoint for ``session_data`` writes from this
        class so the "create if missing" logic and the
        upsert-with-merge logic live in one place — extending to
        a third key (e.g. ``mcp_overrides``) is a one-line
        addition then. Callers handle their own exception
        logging at the public-method level.

        Serialized via ``self._session_data_lock`` so two callers
        writing different keys concurrently can't clobber each
        other on the merge step.
        """

        async with self._session_data_lock:
            session = await self.db.get_session(
                session_id=self.session_id,
                session_type=SessionType.AGENT,
                deserialize=True,
            )
            if session is None:
                # Fresh session that's never been through Agno's
                # run path yet. Create a minimal AgentSession so
                # the upsert lands as an INSERT — every other field
                # is Optional in Agno's dataclass, and the run path
                # will fill them in on the first ``run_message``.
                now = int(time.time())
                session = AgentSession(
                    session_id=self.session_id,
                    session_data={key: value},
                    created_at=now,
                    updated_at=now,
                )
            else:
                sd = dict(session.session_data or {})
                sd[key] = value
                session.session_data = sd
            await self.db.upsert_session(session, deserialize=True)


def _coerce_todo_snapshot(todos: list[dict]) -> list[dict]:
    """Filter to the wire shape ``{content, status, activeForm}``
    with valid statuses. Used by :meth:`SessionPersistence.save_todos`
    (drop malformed writes) and :meth:`SessionPersistence.load_todos`
    (drop malformed reads from the DB).

    Every survivor round-trips through :class:`TodoItemWire` so the
    output dicts come from one Pydantic definition (Rule 1) —
    if the wire shape ever changes, this stays in lockstep."""
    out: list[dict] = []
    for entry in todos:
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content", "")).strip()
        if not content:
            continue
        status = str(entry.get("status", "pending"))
        if status not in ("pending", "in_progress", "completed"):
            continue
        active_form = str(entry.get("activeForm", "") or "")
        out.append(
            TodoItemWire(
                content=content,
                status=status,
                active_form=active_form,
            ).model_dump(by_alias=True)
        )
    return out
