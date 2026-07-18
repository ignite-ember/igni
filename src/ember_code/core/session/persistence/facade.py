"""Thin facade over the persistence stores.

:class:`SessionPersistence` owns one instance each of the six
coordinators built on top of a shared :class:`SessionDataService`
and forwards the historic public methods (``list_sessions`` /
``auto_name`` / ``rename`` / ``fork`` / ``get_name`` /
``_get_name_raw`` / ``load_plan_decisions`` / ``load_todos`` /
``load_event_log`` / ``save_event_log`` / ``save_todos`` /
``save_plan_decisions``) so external callers (the 30+ ``session.
persistence.<method>`` sites across the backend / CLI / tests)
keep working unchanged.

The facade UNWRAPS the internal Pattern-3 envelopes
(:class:`LoadResult` / :class:`PersistResult` / :class:`ForkResult`)
back to the historic dict / list / None-return shapes at the
public boundary. That gives us the Rule 1 / Pattern 3 win
INTERNALLY without a mass caller migration in the same PR — a
follow-up can flip the public API once the internal shape has
stabilised. The one exception is :meth:`save_plan_decisions`,
which re-raises on write failure so :class:`PlanCoordinator`'s
persist-then-flip invariant path can honour the ``ok=False`` →
"abort mode flip" contract.

Rule 6 (OOP): the god-class from the pre-refactor
``persistence.py`` dissolves into six ~30-70 LoC coordinators +
one ~90 LoC service; this facade is the assembly point that
threads them together and preserves the public API surface.
"""

from __future__ import annotations

import logging
from typing import Any

from ember_code.core.session.event_log_schema import SessionEvent
from ember_code.core.session.persistence.data_service import SessionDataService
from ember_code.core.session.persistence.db_protocol import AgnoSessionDb
from ember_code.core.session.persistence.event_log_store import EventLogStore
from ember_code.core.session.persistence.forker import SessionForker
from ember_code.core.session.persistence.listing import SessionListing
from ember_code.core.session.persistence.namer import SessionNamer
from ember_code.core.session.persistence.plan_decisions_store import (
    PlanDecisionsStore,
)
from ember_code.core.session.persistence.todos_store import TodoSnapshotStore
from ember_code.core.session.schemas import SessionTitle
from ember_code.core.tools.plan import PlanDecisionsBlob
from ember_code.core.tools.todo import TodoItemWire

logger = logging.getLogger(__name__)


class SessionPersistence:
    """Handles session listing, naming, and metadata via Agno's DB.

    Thin facade over six store coordinators — each owns one
    concern (listing / naming / forking / plan-decisions / todos /
    event-log) and composes a shared :class:`SessionDataService`
    for the read/write chokepoint against ``session_data``.

    Every historic public method is forwarded to the appropriate
    store; the facade unwraps the internal envelopes to the
    dict / list / ``None`` shapes callers used to see.
    """

    # Deprecated alias — pre-refactor code reached for the nested
    # ``SessionPersistence.SessionTitle`` class via
    # ``self.SessionTitle(...)``. Grep confirms no external caller
    # currently uses it, but the alias is kept for one release
    # cycle as a cheap safety net.
    SessionTitle = SessionTitle

    def __init__(self, db: Any, session_id: str) -> None:
        # ``db: Any`` at the constructor boundary keeps the
        # 30+ existing call sites (backend / CLI / tests that pass
        # a ``MagicMock`` or the real ``AsyncSqliteDb``) working
        # without an explicit protocol conformance check. The
        # underlying :class:`AgnoSessionDb` protocol governs what
        # each store actually calls into.
        self._db: AgnoSessionDb | None = db
        self._session_id = session_id
        self._data = SessionDataService(db, session_id)
        self._listing = SessionListing(db)
        self._namer = SessionNamer(db, session_id, self._data)
        self._forker = SessionForker(db, session_id)
        self._plan_store = PlanDecisionsStore(self._data)
        self._todos_store = TodoSnapshotStore(self._data)
        self._event_log_store = EventLogStore(self._data)

    # ── Legacy attribute exposure ──────────────────────────────
    #
    # Existing callers reach for ``.db`` / ``.session_id`` as
    # public attributes (e.g. :class:`SessionIdentity` mutates
    # ``.session_id`` during a rotate). The properties preserve
    # that surface and thread mutations through to the underlying
    # stores.

    @property
    def db(self) -> AgnoSessionDb | None:
        return self._db

    @property
    def session_id(self) -> str:
        return self._session_id

    @session_id.setter
    def session_id(self, new_id: str) -> None:
        """Retarget every store at the new session id.

        Mirrors the pre-refactor ``self.session_id = new_id``
        write that :class:`SessionIdentity._rotate_id` performs
        after a fork — the previous god-class stored a single
        string, this facade fans the change out to every store.
        """
        self._session_id = new_id
        self._data.session_id = new_id
        self._namer.rebind(new_id)
        self._forker.rebind(new_id)

    # ── Session listing ────────────────────────────────────────

    async def list_sessions(self, limit: int | None = None) -> list[dict[str, Any]]:
        """List sessions from the Agno database.

        Returns the historic ``list[dict]`` wire shape by dumping
        each :class:`SessionListRow` at the facade boundary. On
        DB failure the facade preserves the pre-refactor
        "swallow-and-default-to-empty" contract.
        """
        result = await self._listing.list_sessions(limit=limit)
        rows = result.value or []
        return [row.model_dump() for row in rows]

    # ── Session name ───────────────────────────────────────────

    async def auto_name(self, executor: Any) -> str:
        """Ask Agno to auto-generate a session name from
        conversation. Returns the (cleaned) name, or ``""`` on
        failure."""
        return await self._namer.auto_name(executor)

    async def rename(self, new_name: str) -> None:
        """Manually rename the current session. Best-effort — DB
        failures log at DEBUG and return silently."""
        await self._namer.rename(new_name)

    async def get_name(self) -> str:
        """Get the current session's name from the database."""
        return await self._namer.get_name()

    async def _get_name_raw(self) -> str:
        """Read the persisted session name verbatim (no cleanup).

        Underscore-prefixed on the facade — historic private API
        that :class:`SessionNamer.auto_name` calls internally.
        Kept exposed here for tests that exercise the naming
        pipeline at the persistence layer directly.
        """
        return await self._namer._get_name_raw()

    # ── Fork ──────────────────────────────────────────────────

    async def fork(self, name: str | None = None) -> str:
        """Clone the current session under a fresh ``session_id``.

        Preserves the pre-refactor ``RuntimeError`` semantics on
        failure so existing callers (`/fork` slash command,
        :class:`ForkedSessionRebinder`) keep their
        ``except`` branches valid. Successful returns yield the
        8-char new session id.
        """
        result = await self._forker.fork(name=name)
        if not result.ok:
            raise RuntimeError(result.error or "fork failed")
        return result.new_session_id

    # ── Plan decisions ─────────────────────────────────────────

    async def load_plan_decisions(self) -> dict[str, str]:
        """Read the persisted ``{run_id: "approved"|"dismissed"}``
        map. Empty dict on absence or DB failure — callers treat
        absence as "no decision", the safe pending default.
        """
        result = await self._plan_store.load()
        blob = result.value
        if blob is None:
            return {}
        return blob.model_dump(mode="json")["decisions"]

    async def save_plan_decisions(self, decisions: PlanDecisionsBlob | dict[str, str]) -> None:
        """Write the ``{run_id: decision}`` map to
        ``session_data['plan_decisions']``.

        Accepts either the typed :class:`PlanDecisionsBlob`
        (post-refactor caller shape from
        :class:`PlanCoordinator`) or the raw ``dict[str, str]``
        (legacy test fixture shape). Both materialise a
        :class:`PlanDecisionsBlob` internally so the on-disk shape
        stays a plain ``dict[str, str]``.

        Re-raises on write failure so :class:`PlanCoordinator`
        can honour the persist-then-flip invariant (approve path
        aborts the mode flip if the write fails; dismiss path
        swallows). Every other public method on this facade
        swallows — plan decisions are the only case where the
        caller must know the write actually landed.
        """
        if isinstance(decisions, PlanDecisionsBlob):
            blob = decisions
        else:
            blob = PlanDecisionsBlob.from_raw(decisions)
        result = await self._plan_store.save(blob)
        if not result.ok:
            raise RuntimeError(result.error or "save_plan_decisions failed")

    # ── Todos ─────────────────────────────────────────────────

    async def load_todos(self) -> list[dict]:
        """Read the persisted todo snapshot for this session.

        Returns the historic ``list[dict]`` shape (each entry
        ``{content, status, activeForm}``) by dumping each
        :class:`TodoItemWire` at the facade boundary. Malformed
        rows drop silently at the store's coercion step.
        """
        result = await self._todos_store.load()
        wires = result.value or []
        return [w.model_dump(by_alias=True) for w in wires]

    async def save_todos(self, todos: list[dict]) -> None:
        """Atomic-replace persisted todo snapshot.

        Accepts the pre-refactor ``list[dict]`` shape from
        :meth:`TodoTools._persist_state` / test fixtures.
        Coercion via :meth:`TodoItemWire.coerce_snapshot` filters
        malformed entries. Best-effort: DB write failures log and
        return.
        """
        wires = TodoItemWire.coerce_snapshot(todos)
        await self._todos_store.save(wires)

    # ── Event log ────────────────────────────────────────────

    async def load_event_log(self) -> list[dict]:
        """Read the persisted append-only event log.

        Returns the historic ``list[dict]`` shape by dumping each
        :class:`SessionEvent`. Every persisted entry has been
        round-tripped through :meth:`SessionEvent.from_wire` — stale
        rows drop silently at the store's coercion step, so the
        output dicts come from one Pydantic definition (Rule 1).
        """
        result = await self._event_log_store.load()
        events = result.value or []
        return [e.model_dump() for e in events]

    async def save_event_log(self, event_log: list[dict] | list[Any]) -> None:
        """Atomic-replace the session's event log.

        Accepts either the historic ``list[dict]`` (from
        :meth:`SessionEventLog.append` before this refactor
        pass) OR a ``list[SessionEvent]`` (typed callers). The
        facade coerces the mixed input to a homogenous
        ``list[SessionEvent]`` before delegating to the store —
        so the store's typed contract holds regardless of caller
        shape.
        """
        events: list[SessionEvent] = []
        for entry in event_log:
            if isinstance(entry, SessionEvent):
                events.append(entry)
            elif isinstance(entry, dict):
                evt = SessionEvent.from_wire(entry)
                if evt is not None:
                    events.append(evt)
        await self._event_log_store.save(events)
